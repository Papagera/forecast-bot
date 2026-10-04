"""Агентный исследователь (блок 2.0): модель сама решает, что искать, читает страницы, берёт ряды.

Попытка воспроизвести подход metac-agent / metac-azimuth («agentic forecasting harness» команды Metaculus;
по метаданным API — Claude Opus 5.5 High). Их код закрыт, см. `_отчёты/отчёт-2026-10-02-блок-2.0-агенты.md`.

Цикл: до MAX_STEPS ходов модели с инструментами → итоговая справка исследования (без прогноза);
прогноз делает прогнозист шаблона. Все вызовы модели — `guarded_completion` (ai_guard + сторож).
Деньги: мягкий стоп — когда вопрос потратил ≥ SOFT_STOP_SHARE от бюджета вопроса, инструменты
отключаются и модель обязана дописать справку тем, что уже собрано.
"""
from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import requests

from forecast_bot import ai_guard, guarded_llm, verify

MAX_STEPS = 6
MAX_NEWS_CALLS = 3          # AskNews «свежие» = 1 вызов каждый (квота)
TOOL_TEXT_LIMIT = 3500      # символов на ответ инструмента — держит рост контекста и цену
SOFT_STOP_SHARE = 0.6

TOOLS = [
    {"type": "function", "function": {
        "name": "search_news",
        "description": "Search the web (recent news, official pages, data) about a topic. Returns page titles, URLs "
                       "and excerpts.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch a public web page (http/https) and return its visible text, truncated.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "fred_series",
        "description": "Get a FRED economic data series by id (e.g. UNRATE, CPIAUCSL, DGS10): recent observations and summary stats.",
        "parameters": {"type": "object", "properties": {"series_id": {"type": "string"}}, "required": ["series_id"]}}},
    {"type": "function", "function": {
        "name": "stock_history",
        "description": "Daily closes from Yahoo Finance for a ticker (e.g. 'AAPL', '^GSPC' for S&P 500, '^VIX', "
                       "'EURUSD=X', 'GC=F' gold futures, 'BTC-USD'): recent closes and volatility.",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
]

SYSTEM = """You are the research assistant of a superforecaster. You have tools; use them to find what matters
for the question: current status, the most recent relevant data points, base rates of similar events, scheduled
events before the resolution date, and how the resolution source will measure the outcome. For questions about a
data series (economic indicators, prices, counts), fetch the series itself and report its latest value, recent
trend and typical volatility over a horizon like the question's. Be efficient: at most {steps} tool rounds.
Do NOT give a final probability. Finish with a concise research brief (<= 500 words): key facts with dates,
base rates, the status quo outcome, and the main uncertainties.

SOURCES RULE (strict): every tool result is labelled with a source id like [S3]; the question itself is [S0].
Write the brief as one fact per line. Every line that contains a number MUST end with the id(s) of the source(s)
where that exact number appears, e.g. "- Unemployment was 4.2% in August 2026 [S2]". Copy numbers exactly as
written in the source. Never estimate, round, convert units or recall numbers from memory: a number that does not
literally appear in a cited source will be deleted together with its line. Facts without numbers need no id."""


# ─────────────────────────── инструменты ────────────────────────────
def _clip(text: str) -> str:
    return text if len(text) <= TOOL_TEXT_LIMIT else text[:TOOL_TEXT_LIMIT] + "\n…[обрезано]"


def _is_public_http(url: str) -> bool:
    """Только http/https на публичные адреса: агент не должен ходить в локальную сеть машины."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def fetch_url(url: str) -> str:
    if not _is_public_http(url):
        return "Отказ: разрешены только публичные http/https адреса."
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 forecast-bot research"},
                         stream=True)
        raw = r.raw.read(400_000, decode_content=True)
    except Exception as exc:  # сетевая ошибка — ответ модели, не падение агента
        return f"Ошибка загрузки: {type(exc).__name__}"
    text = raw.decode(r.encoding or "utf-8", errors="ignore")
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return _clip(f"HTTP {r.status_code}. {text.strip()}")


def _series_stats(dates: list[str], values: list[float], label: str) -> str:
    if not values:
        return f"{label}: данных нет."
    tail = list(zip(dates, values))[-15:]
    changes = [b - a for a, b in zip(values, values[1:])]
    sd = (sum((c - sum(changes) / len(changes)) ** 2 for c in changes) / max(1, len(changes) - 1)) ** 0.5 if len(changes) > 1 else 0
    lines = [f"{label}: {len(values)} наблюдений, {dates[0]} … {dates[-1]}",
             f"последнее {values[-1]} ({dates[-1]}), мин {min(values)}, макс {max(values)}",
             f"ст. откл. изменения за шаг: {sd:.4g}",
             "последние: " + ", ".join(f"{d}={v}" for d, v in tail)]
    return _clip("\n".join(lines))


def fred_series(series_id: str) -> str:
    sid = re.sub(r"[^A-Za-z0-9_]", "", series_id)[:40]
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid}, timeout=20)
        rows = list(csv.reader(io.StringIO(r.text)))
    except Exception as exc:
        return f"Ошибка FRED: {type(exc).__name__}"
    dates, vals = [], []
    for row in rows[1:]:
        try:
            dates.append(row[0]); vals.append(float(row[1]))
        except (ValueError, IndexError):
            continue
    return _series_stats(dates[-400:], vals[-400:], f"FRED {sid}")


def stock_history(symbol: str) -> str:
    """Дневные закрытия с Yahoo Finance chart API (без ключа). Stooq с 10.2026 отдаёт JS-проверку браузера вместо
    данных — прежний инструмент молча возвращал пустоту, агент тратил на него шаги."""
    sym = re.sub(r"[^A-Za-z0-9.^=_-]", "", symbol)[:20].upper()
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         params={"range": "1y", "interval": "1d"},
                         headers={"User-Agent": "Mozilla/5.0 forecast-bot research"}, timeout=20)
        res = r.json()["chart"]["result"][0]
    except Exception as exc:  # не JSON, нет тикера, сеть — ответ модели, не падение агента
        return f"Ошибка Yahoo для {sym}: {type(exc).__name__} — проверь тикер (AAPL, ^GSPC, EURUSD=X, GC=F, BTC-USD)."
    tz = res.get("meta", {}).get("gmtoffset", 0)
    closes = res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    dates, vals = [], []
    for ts, cl in zip(res.get("timestamp") or [], closes):
        if cl is None:
            continue
        dates.append(datetime.fromtimestamp(ts + tz, tz=timezone.utc).date().isoformat())
        vals.append(round(float(cl), 6))
    return _series_stats(dates[-260:], vals[-260:], f"Yahoo {sym}")


# ─────────────────────────── цикл агента ────────────────────────────
class ResearchAgent:
    def __init__(self, model: str, *, question_budget_usd: float = 0.30,
                 news: Callable[[str], Awaitable[str]] | None = None,
                 max_tokens: int = 4000, max_news: int = MAX_NEWS_CALLS) -> None:
        self.model = model
        self.max_news = max_news
        self.question_budget_usd = question_budget_usd
        self.news = news
        self.max_tokens = max_tokens
        self.news_calls = 0
        self.news_failed = False
        self.sources: dict[str, str] = {}
        self.raw_brief = ""
        self.verdict: verify.Verdict | None = None

    def _spent(self, since: float) -> float:
        user = f"{guarded_llm.APP}:{guarded_llm.CURRENT_USER.get()}"
        return ai_guard.spent_by_user(user, since)[0]

    async def _run_tool(self, name: str, args: dict) -> str:
        """Сбой инструмента — ответ модели, а не падение вопроса. 05.10.2026 кошелёк AskNews кончился
        (APIError 402001): исключение из поиска роняло исследование целиком, и вопрос уходил в error без прогноза."""
        try:
            return await self._run_tool_inner(name, args)
        except Exception as exc:
            if name == "search_news":
                self.news_failed = True  # дальше на этом вопросе поиск не пробуем
            return f"Инструмент {name} недоступен ({type(exc).__name__}: {str(exc)[:120]}). Продолжай без него."

    async def _run_tool_inner(self, name: str, args: dict) -> str:
        if name == "search_news":
            if self.news is None or self.news_failed or self.news_calls >= self.max_news:
                return "Поиск новостей недоступен (исчерпан лимит вызовов на вопрос или сервис не отвечает)."
            self.news_calls += 1
            return _clip(await self.news(str(args.get("query", ""))[:300]))
        if name == "fetch_url":
            return await asyncio.to_thread(fetch_url, str(args.get("url", "")))
        if name == "fred_series":
            return await asyncio.to_thread(fred_series, str(args.get("series_id", "")))
        if name == "stock_history":
            return await asyncio.to_thread(stock_history, str(args.get("symbol", "")))
        return f"Неизвестный инструмент {name}."

    async def research(self, question: Any) -> str:
        """Справка, прошедшая сверку чисел (`verify.check`); сырой текст — в `self.raw_brief`."""
        self.raw_brief = await self._loop(question)
        self.verdict = verify.check(self.raw_brief, self.sources)
        return self.verdict.text

    def _label(self, name: str, args: dict, result: str) -> str:
        sid = f"S{len(self.sources)}"
        self.sources[sid] = result
        detail = args.get("url") or args.get("query") or args.get("series_id") or args.get("symbol") or ""
        return f"[{sid}] source: {name} {detail}\n{result}"

    async def _loop(self, question: Any) -> str:
        t0 = time.time()
        self.news_calls = 0
        import os

        today = os.environ.get("FORECAST_ASOF", "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        user_msg = (f"Today is {today}.\nQuestion: {question.question_text}\n\n"
                    f"Resolution criteria: {question.resolution_criteria}\n\nFine print: {question.fine_print}\n\n"
                    f"Background: {question.background_info}\n\nQuestion closes: {question.close_time}; "
                    f"resolves: {question.scheduled_resolution_time}.")
        self.sources = {"S0": user_msg}
        messages: list[dict] = [{"role": "system", "content": SYSTEM.format(steps=MAX_STEPS)},
                                {"role": "user", "content": "[S0] question\n" + user_msg}]
        for step in range(MAX_STEPS + 1):
            over_budget = self._spent(t0) >= SOFT_STOP_SHARE * self.question_budget_usd
            last = step == MAX_STEPS or over_budget
            if last:
                messages.append({"role": "user", "content": "Stop using tools now. Write the final research brief."})
            # tools передаём и на последнем шаге: Anthropic требует их, если в истории есть tool_use.
            kwargs: dict[str, Any] = {"tools": TOOLS, "tool_choice": "none" if last else "auto"}
            resp = await guarded_llm.guarded_completion(self.model, messages, max_tokens=self.max_tokens,
                                                        temperature=0.2, **kwargs)
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            if last or not calls:
                return (msg.content or "").strip() or "Исследование не дало текста."
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": c.id, "type": "function",
                                             "function": {"name": c.function.name,
                                                          "arguments": c.function.arguments}} for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self._run_tool(c.function.name, args)
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": self._label(c.function.name, args, result)})
        return "Исследование не завершено."
