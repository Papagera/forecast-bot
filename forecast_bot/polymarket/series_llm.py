"""Модель (b) бэктеста №2: quant + поправка LLM по заголовкам GDELT строго до t.

Один вызов на группу (событие × точка; у крипты «above» и «price on» одной даты — общая группа): LLM видит
базовый расчёт (последнее значение, горизонт, разброс, вероятности quant по страйкам) и заголовки, но НЕ цену рынка.
Ответ — JSON {shift_sigma ∈ [−1.5, 1.5], vol_mult ∈ [0.5, 2.0], reason}; применяется ко всей сетке страйков
(`series_quant.prob(..., shift_sigma, vol_mult)`), поэтому страйки не противоречат друг другу.
Вызов — через `guarded_llm.guarded_completion` (ai_guard, приложение `polymarket`, пользователь `pm2:<группа>`).
Непарсящийся ответ → ошибка группы, без подстановки нулевой поправки.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

SHIFT_MAX = 1.5
VOL_RANGE = (0.5, 2.0)
MAX_TOKENS = 300
DEFAULT_MODEL = "openrouter/anthropic/claude-opus-5.5"

UNDERLYING = {
    "binance:BTCUSDT": "Bitcoin price, Binance BTC/USDT (USD)",
    "binance:ETHUSDT": "Ethereum price, Binance ETH/USDT (USD)",
    "yahoo:DX-Y.NYB": "US Dollar Index (DXY)",
    "yahoo:CL=F": "WTI crude oil, front-month futures (USD/bbl)",
    "macro:cpi_mom": "US CPI-U, one-month % change, seasonally adjusted (BLS)",
    "macro:core_mom": "US core CPI-U (ex food & energy), one-month % change, SA (BLS)",
    "macro:cpi_yoy": "US CPI-U, 12-month % change, not seasonally adjusted (BLS)",
    "macro:core_yoy": "US core CPI-U, 12-month % change, NSA (BLS)",
    "macro:jolts": "US JOLTS job openings, total nonfarm, SA (thousands)",
}

QUERY = {  # слова для подбора заголовков GDELT (gdelt_files.words_for берёт до 6 ключевых)
    "binance:BTCUSDT": "Bitcoin BTC crypto price",
    "binance:ETHUSDT": "Ethereum ETH crypto price",
    "yahoo:DX-Y.NYB": "dollar index DXY Fed currency",
    "yahoo:CL=F": "oil crude WTI OPEC prices",
    "macro:cpi_mom": "inflation CPI consumer prices",
    "macro:core_mom": "core inflation CPI consumer prices",
    "macro:cpi_yoy": "inflation CPI consumer prices",
    "macro:core_yoy": "core inflation CPI consumer prices",
    "macro:jolts": "JOLTS job openings labor market",
}


def underlying(key: str) -> str:
    if key.startswith("treasury:"):
        return f"US Treasury {key.split(':', 1)[1].replace(' Yr', '-year')} par yield (%, daily, Treasury.gov)"
    return UNDERLYING.get(key, key)


def query_for(key: str) -> str:
    if key.startswith("treasury:"):
        return "Treasury yields bonds Fed"
    return QUERY.get(key, key)


@dataclass
class GroupView:
    """Всё, что видит LLM. Цены рынка здесь нет по построению — её нет и в промпте."""
    key: str
    t: datetime
    titles: list[str]
    rule: str
    baseline: str                       # строка про s0 / горизонт / разброс
    strikes: list[tuple[str, float]]    # (условие, вероятность quant)
    news: str
    unit_note: str = ""
    extra: dict = field(default_factory=dict)


def build_messages(g: GroupView) -> list[dict]:
    lines = [
        f"Forecast date: {g.t:%Y-%m-%d %H:%M} UTC. Treat this moment as now; you know nothing after it.",
        f"Underlying: {underlying(g.key)}.",
        "Prediction markets on this underlying: " + "; ".join(g.titles[:3]) + ".",
        f"Resolution rule (excerpt): {g.rule[:500]}",
        "",
        "Statistical baseline (history of the series only, no news):",
        g.baseline,
        "Baseline probabilities:",
        *[f"- {cond}: {p:.3f}" for cond, p in g.strikes[:16]],
        "",
        g.news,
        "",
        "Task: decide whether the information above justifies adjusting the baseline distribution of the outcome.",
        "Return JSON only, no prose: {\"shift_sigma\": number, \"vol_mult\": number, \"reason\": \"one sentence\"}.",
        f"- shift_sigma in [-{SHIFT_MAX}, {SHIFT_MAX}]: expected change of the outcome relative to the baseline, "
        "in baseline standard deviations (positive = higher value).",
        f"- vol_mult in [{VOL_RANGE[0]}, {VOL_RANGE[1]}]: scale of baseline uncertainty "
        "(above 1 for scheduled catalysts or turmoil, below 1 if unusually calm).",
        "- If the headlines carry no concrete relevant information, return shift_sigma 0 and vol_mult 1.",
    ]
    if g.unit_note:
        lines.insert(2, g.unit_note)
    return [
        {"role": "system", "content": "You are a careful quantitative forecaster. You adjust a statistical "
                                      "baseline using only information available at the forecast date."},
        {"role": "user", "content": "\n".join(lines)},
    ]


class BadAnswer(ValueError):
    pass


def parse_answer(text: str) -> tuple[float, float, str]:
    """(shift_sigma, vol_mult, reason) с клипом в допустимые рамки. Нет JSON / нет чисел → BadAnswer."""
    found = re.findall(r"\{[^{}]*\}", text or "", re.S)
    if not found:
        raise BadAnswer("в ответе нет JSON")
    try:
        d = json.loads(found[-1])
        shift, vol = float(d["shift_sigma"]), float(d["vol_mult"])
    except (ValueError, KeyError, TypeError) as exc:
        raise BadAnswer(f"JSON без чисел: {exc}") from exc
    if shift != shift or vol != vol:  # NaN
        raise BadAnswer("NaN")
    shift = max(-SHIFT_MAX, min(SHIFT_MAX, shift))
    vol = max(VOL_RANGE[0], min(VOL_RANGE[1], vol))
    return shift, vol, str(d.get("reason", ""))[:300]


async def adjust(g: GroupView, user: str) -> tuple[float, float, str]:
    """Вызов LLM через гард. `user` — ключ леджера внутри приложения (`pm2:<группа>-<точка>`)."""
    from forecast_bot import guarded_llm

    guarded_llm.install_sentinel()
    model = os.environ.get("FORECAST_MODEL", DEFAULT_MODEL)
    token = guarded_llm.CURRENT_USER.set(user)
    try:
        resp = await guarded_llm.guarded_completion(model, build_messages(g), max_tokens=MAX_TOKENS, temperature=0)
    finally:
        guarded_llm.CURRENT_USER.reset(token)
    text = resp.choices[0].message.content or ""
    return parse_answer(text)
