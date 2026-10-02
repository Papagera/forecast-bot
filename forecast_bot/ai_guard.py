# -*- coding: utf-8 -*-
# КОПИЯ: clipper/clipper/vendor/ai_guard.py (сама — копия ezcar-media-engine/code/ai_guard.py),
# скопировано 02.10.2026. Оригинал не править (§3). Дрейф от оригинала принят осознанно (ТЗ этапа 1 §3).
#
# Правки forecast-bot (аддитивные, схема ОБЩЕГО ledger не тронута):
#   1. `acall()` — async-вход для forecasting-tools (там всё на asyncio). Порядок и проверки те же,
#      что у `call()`: kill-switch → breaker → `_precheck` → вызов → `_record` по факту токенов.
#   2. Цены OpenRouter-моделей бота (openrouter.ai/api/v1/models, 02.10.2026, $/1M → $/1k).
#   3. APP_LIMITS["forecast"] = $3/день — потолок бота внутри общего лимита машины (income, 02.10.2026).
#   6. TokenUsage.actual_cost_usd — фактическая стоимость из ответа (OpenRouter usage.cost);
#      если провайдер её не дал (0/None) — расчёт по PRICES, как в оригинале.
#   4. LIMITS day/month = $50/$500 — канон §4b EZCAR_SHARED_RULES (с 21.08.2026,
#      ezcar-analytics/ai_guard.py:74-75). В копии clipper стояли устаревшие $10/$200.
#   5. Убраны `call_flat`/FLAT_PRICES/INTRO_PRICES и чужие APP-лимиты — боту не нужны.
"""ai_guard — общий шлюз для ВСЕХ платных ИИ-вызовов (§4b SHARED_RULES).

Гард:
  1. Кэш проверяется ДО платного вызова и отдаётся ВСЕГДА (даже при превышении лимита).
  2. HARD-стоп (`BudgetExceeded`) при превышении ЛЮБОГО лимита — ДО вызова.
  3. Ограничение размера запроса (per-call $0.50 — ловит токен-бомбы).
  4. Учёт по ФАКТУ токенов из `usage` (не по числу вызовов).

Kill-switch: `AI_KILL=1`. Circuit-breaker: 5×(429/5xx) подряд → пауза 10 мин на провайдера.
Ledger — ОДИН SQLite на машину: `~/.ezcar/ai_ledger.db` (WAL), env `AI_LEDGER_PATH`, ВНЕ репо.
Приложение кодируется префиксом поля `user`: `"forecast:q<id>"`.
"""
from __future__ import annotations

import datetime
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol


class BudgetExceeded(Exception):
    """Любой лимит превышен / kill-switch / breaker — вызов HARD-отклонён (кэш всё равно отдан)."""


class AIGuardError(Exception):
    """Ошибка конфигурации гарда (напр. неизвестна цена модели)."""


# ─────────────────────────── лимиты и цены ──────────────────────────
LIMITS = {
    "day_usd": 50.0,          # суммарно по всем моделям, per-machine (канон §4b)
    "month_usd": 500.0,
    "per_call_usd": 0.50,     # ловит токен-бомбы (pre-check по max_tokens)
    "rpm": 30,                # запросов за скользящие 60 c
    "tpm": 150_000,           # токенов за скользящие 60 c
    "per_user_day_calls": int(os.environ.get("AI_USER_DAY_CALLS", "20")),
    "breaker_fails": 5,
    "breaker_pause_s": 600,
    "breaker_window_s": 600,
}

PROVIDER_LIMITS = {
    "google": {"day_calls": 200, "day_usd": None, "month_usd": None},
    "openai": {"day_calls": None, "day_usd": 3.0, "month_usd": 30.0},
    "anthropic": {"day_calls": None, "day_usd": 5.0, "month_usd": 100.0},
}

APP_LIMITS = {
    # forecast-bot (личное, вне EZCAR): потолок $3/день — указание income 02.10.2026.
    "forecast": {"day_usd": 3.0},
    # Замеры вариантов на Mac (блок 2.1, income 03.10.2026): 4 варианта × ~16 вопросов ≈ $5 (≈оценка).
    "forecast-lab": {"day_usd": 8.0},
}

APP_PROVIDER_LIMITS: dict[str, dict[str, dict[str, float]]] = {}

# Цены $/1k токенов (in, out).
PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-4o-mini"): (0.00015, 0.00060),
    ("openai", "gpt-4o"): (0.0025, 0.01),
    ("anthropic", "claude-opus-5-5"): (0.004, 0.020),
    ("anthropic", "claude-sonnet-5-5"): (0.002, 0.010),
    # forecast-bot: openrouter.ai/api/v1/models, 02.10.2026 (pricing.prompt/completion × 1000).
    ("openrouter", "openrouter/anthropic/claude-opus-5.5"): (0.004, 0.020),
    ("openrouter", "openrouter/anthropic/claude-sonnet-5.5"): (0.002, 0.010),
    ("openrouter", "openrouter/openai/gpt-4o-mini"): (0.00015, 0.0006),
    ("openrouter", "openrouter/openai/gpt-4o"): (0.0025, 0.01),
    ("openrouter", "openrouter/google/gemini-3.5-flash"): (0.0015, 0.009),
    ("openrouter", "openrouter/anthropic/claude-haiku-4.5"): (0.001, 0.005),
    ("openrouter", "openrouter/google/gemini-3.1-pro-preview"): (0.002, 0.012),
}

CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = 1.25
CACHE_WRITE_MULT_1H = 2.0


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: bool = False
    # forecast-bot: фактическая стоимость из ответа провайдера (OpenRouter `usage.cost`).
    # Задана → в ledger идёт она, а не расчёт по PRICES (цена из PRICES нужна только для pre-check).
    actual_cost_usd: Optional[float] = None

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def _price_for(provider: str, model: str, now: float) -> Optional[tuple[float, float]]:
    return PRICES.get((provider, model))


def _cost(usage: TokenUsage, price: tuple[float, float]) -> float:
    if usage.actual_cost_usd is not None and usage.actual_cost_usd > 0:
        return float(usage.actual_cost_usd)
    p_in, p_out = price
    write_mult = CACHE_WRITE_MULT_1H if usage.cache_write_1h else CACHE_WRITE_MULT
    return (
        usage.input / 1000 * p_in
        + usage.output / 1000 * p_out
        + usage.cache_read / 1000 * p_in * CACHE_READ_MULT
        + usage.cache_write / 1000 * p_in * write_mult
    )


# ─────────────────────────── ledger ─────────────────────────────────
def _ledger_path() -> str:
    p = os.environ.get("AI_LEDGER_PATH")
    if p:
        return p
    return str(Path.home() / ".ezcar" / "ai_ledger.db")


def _conn() -> sqlite3.Connection:
    path = _ledger_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage (
            ts REAL, provider TEXT, model TEXT, "user" TEXT,
            tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS breaker (
            provider TEXT PRIMARY KEY, fails INTEGER DEFAULT 0, paused_until REAL DEFAULT 0,
            last_fail_ts REAL DEFAULT 0
        )"""
    )
    try:
        conn.execute("ALTER TABLE breaker ADD COLUMN last_fail_ts REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    return conn


def _day_start(now: float) -> float:
    d = datetime.datetime.fromtimestamp(now)
    return datetime.datetime(d.year, d.month, d.day).timestamp()


def _month_start(now: float) -> float:
    d = datetime.datetime.fromtimestamp(now)
    return datetime.datetime(d.year, d.month, 1).timestamp()


def _sum_cost(conn, since: float, provider: Optional[str] = None) -> float:
    q = "SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE ts >= ?"
    args: list[Any] = [since]
    if provider:
        q += " AND provider = ?"
        args.append(provider)
    return float(conn.execute(q, args).fetchone()["s"])


def _count_calls(conn, since: float, provider: Optional[str] = None) -> int:
    q = "SELECT COUNT(*) n FROM usage WHERE ts >= ?"
    args: list[Any] = [since]
    if provider:
        q += " AND provider = ?"
        args.append(provider)
    return int(conn.execute(q, args).fetchone()["n"])


def _sum_tokens(conn, since: float) -> int:
    r = conn.execute(
        "SELECT COALESCE(SUM(tokens_in+tokens_out),0) t FROM usage WHERE ts >= ?", (since,)
    ).fetchone()
    return int(r["t"])


def _user_calls_today(conn, user: str, day_start: float) -> int:
    return int(
        conn.execute(
            'SELECT COUNT(*) n FROM usage WHERE ts >= ? AND "user" = ?', (day_start, user)
        ).fetchone()["n"]
    )


def _app_cost_today(conn, app: str, day_start: float) -> float:
    r = conn.execute(
        'SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE ts >= ? AND "user" LIKE ?',
        (day_start, f"{app}:%"),
    ).fetchone()
    return float(r["s"])


def _app_provider_cost(conn, app: str, provider: str, since: float) -> float:
    row = conn.execute(
        'SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE ts >= ? AND provider = ? AND "user" LIKE ?',
        (since, provider, f"{app}:%"),
    ).fetchone()
    return float(row[0])


def _check_app_provider(conn, app: Optional[str], provider: str, now: float) -> None:
    lim = (APP_PROVIDER_LIMITS.get(app) or {}).get(provider) if app else None
    if not lim:
        return
    if lim.get("day_usd") and _app_provider_cost(conn, app, provider, _day_start(now)) >= lim["day_usd"]:
        raise BudgetExceeded(f"{app}/{provider}: ${lim['day_usd']}/день исчерпан")
    if lim.get("month_usd") and _app_provider_cost(conn, app, provider, _month_start(now)) >= lim["month_usd"]:
        raise BudgetExceeded(f"{app}/{provider}: ${lim['month_usd']}/мес исчерпан")


# ─────────────────────────── circuit breaker ────────────────────────
def _breaker(conn, provider: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM breaker WHERE provider = ?", (provider,)).fetchone()


def _breaker_fail(conn, provider: str, now: float) -> None:
    """Окно + обнуление при открытии паузы — см. оригинал (грабли бухгалтера 31.07.2026)."""
    row = _breaker(conn, provider)
    prev = int(row["fails"] or 0) if row else 0
    last = float(row["last_fail_ts"] or 0) if row else 0.0
    if last and now - last > LIMITS["breaker_window_s"]:
        prev = 0
    fails = prev + 1
    paused = 0.0
    if fails >= LIMITS["breaker_fails"]:
        paused = now + LIMITS["breaker_pause_s"]
        fails = 0
    conn.execute(
        "INSERT INTO breaker(provider,fails,paused_until,last_fail_ts) VALUES(?,?,?,?) "
        "ON CONFLICT(provider) DO UPDATE SET fails=excluded.fails, "
        "paused_until=excluded.paused_until, last_fail_ts=excluded.last_fail_ts",
        (provider, fails, paused, now),
    )
    conn.commit()


def _breaker_reset(conn, provider: str) -> None:
    conn.execute(
        "INSERT INTO breaker(provider,fails,paused_until,last_fail_ts) VALUES(?,0,0,0) "
        "ON CONFLICT(provider) DO UPDATE SET fails=0, paused_until=0, last_fail_ts=0",
        (provider,),
    )
    conn.commit()


_TRANSIENT_RE = re.compile(
    r"""
      \b(?:429|5\d\d)\s+(?:Client|Server)\s+Error\b
    | \bHTTP(?:/\d(?:\.\d)?)?\s*(?:status\s*)?[: ]\s*(?:429|5\d\d)\b
    | ["']?\b(?:code|status|status_code|http_status)["']?\s*[:=]\s*(?:429|5\d\d)\b
    | \bRESOURCE_EXHAUSTED\b
    | \bToo\s+Many\s+Requests\b
    | \b(?:Internal\s+Server\s+Error|Service\s+Unavailable|Bad\s+Gateway|Gateway\s+Time-?out)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_transient(exc: BaseException) -> bool:
    code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if code is None:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    if isinstance(code, int) and (code == 429 or 500 <= code <= 599):
        return True
    return bool(_TRANSIENT_RE.search(str(exc)))


class Cache(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...


# ─────────────────────────── pre-check ──────────────────────────────
def _precheck(conn, provider: str, model: str, user: Optional[str], now: float,
              max_tokens: Optional[int], price: tuple[float, float],
              app: Optional[str]) -> None:
    day, month = _day_start(now), _month_start(now)
    minute = now - 60

    if max_tokens is not None:
        est = max_tokens / 1000 * price[1]
        if est > LIMITS["per_call_usd"]:
            raise BudgetExceeded(
                f"per-call ${est:.2f} > ${LIMITS['per_call_usd']} (max_tokens={max_tokens})"
            )

    if _sum_cost(conn, day) >= LIMITS["day_usd"]:
        raise BudgetExceeded(f"дневной лимит ${LIMITS['day_usd']} исчерпан")
    if _sum_cost(conn, month) >= LIMITS["month_usd"]:
        raise BudgetExceeded(f"месячный лимит ${LIMITS['month_usd']} исчерпан")
    if _count_calls(conn, minute) >= LIMITS["rpm"]:
        raise BudgetExceeded(f"RPM {LIMITS['rpm']} превышен")
    if _sum_tokens(conn, minute) >= LIMITS["tpm"]:
        raise BudgetExceeded(f"TPM {LIMITS['tpm']} превышен")
    if user and _user_calls_today(conn, user, day) >= LIMITS["per_user_day_calls"]:
        raise BudgetExceeded(f"per-user {LIMITS['per_user_day_calls']}/день для {user!r}")

    if app:
        al = APP_LIMITS.get(app)
        if al and al.get("day_usd") and _app_cost_today(conn, app, day) >= al["day_usd"]:
            raise BudgetExceeded(f"{app}: дневной лимит ${al['day_usd']} исчерпан")
        _check_app_provider(conn, app, provider, now)

    pl = PROVIDER_LIMITS.get(provider)
    if pl:
        if pl.get("day_calls") and _count_calls(conn, day, provider) >= pl["day_calls"]:
            raise BudgetExceeded(f"{provider}: {pl['day_calls']} вызовов/день")
        if pl.get("day_usd") and _sum_cost(conn, day, provider) >= pl["day_usd"]:
            raise BudgetExceeded(f"{provider}: ${pl['day_usd']}/день")
        if pl.get("month_usd") and _sum_cost(conn, month, provider) >= pl["month_usd"]:
            raise BudgetExceeded(f"{provider}: ${pl['month_usd']}/мес")


def _record(conn, now, provider, model, user, usage: TokenUsage, cost) -> None:
    conn.execute(
        'INSERT INTO usage(ts,provider,model,"user",tokens_in,tokens_out,cost_usd) '
        "VALUES(?,?,?,?,?,?,?)",
        (now, provider, model, user,
         int(usage.input + usage.cache_read + usage.cache_write), int(usage.output), float(cost)),
    )
    conn.commit()


def _normalize(returned: Any) -> tuple[Any, TokenUsage]:
    if len(returned) == 3:
        result, tin, tout = returned
        return result, TokenUsage(input=int(tin), output=int(tout))
    if len(returned) == 2:
        result, usage = returned
        if not isinstance(usage, TokenUsage):
            raise AIGuardError(
                "fn вернула 2 значения — вторым ожидается TokenUsage, "
                f"получено {type(usage).__name__}"
            )
        return result, usage
    raise AIGuardError("fn должна вернуть (result, tokens_in, tokens_out) или (result, TokenUsage)")


def _ledger_user(user: Optional[str], app: Optional[str]) -> Optional[str]:
    return f"{app}:{user}" if app and user else (f"{app}:-" if app else user)


def _gate(provider: str, model: str, user: Optional[str], app: Optional[str],
          max_tokens: Optional[int], price: Optional[tuple[float, float]]):
    """Общая часть call/acall ДО вызова: kill-switch → цена → breaker → лимиты."""
    if os.environ.get("AI_KILL") == "1":
        raise BudgetExceeded("kill-switch AI_KILL=1")
    now = time.time()
    price = price or _price_for(provider, model, now)
    if price is None:
        raise AIGuardError(f"нет цены для {provider}/{model} — задай PRICES или параметр price")
    ledger_user = _ledger_user(user, app)
    conn = _conn()
    try:
        b = _breaker(conn, provider)
        if b and b["paused_until"] and b["paused_until"] > now:
            left = int(b["paused_until"] - now)
            raise BudgetExceeded(f"circuit-breaker {provider} открыт ещё {left} c")
        _precheck(conn, provider, model, ledger_user, now, max_tokens, price, app)
    finally:
        conn.close()
    return now, price, ledger_user


def _after(provider: str, model: str, now: float, price, ledger_user, usage: TokenUsage) -> float:
    cost = _cost(usage, price)
    conn = _conn()
    try:
        _record(conn, now, provider, model, ledger_user, usage, cost)
        _breaker_reset(conn, provider)
    finally:
        conn.close()
    return cost


def _on_fail(provider: str, now: float, exc: BaseException) -> None:
    if _is_transient(exc):
        conn = _conn()
        try:
            _breaker_fail(conn, provider, now)
        finally:
            conn.close()


# ─────────────────────────── публичный API ──────────────────────────
def call(
    provider: str,
    model: str,
    fn: Callable[[], tuple],
    *,
    user: Optional[str] = None,
    app: Optional[str] = None,
    cache: Optional[Cache] = None,
    cache_key: Optional[str] = None,
    max_tokens: Optional[int] = None,
    price: Optional[tuple[float, float]] = None,
) -> Any:
    """Синхронный вход. Порядок: кэш → kill-switch → breaker → лимиты → fn() → учёт факта → кэш.set."""
    if cache is not None and cache_key is not None:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    now, price, ledger_user = _gate(provider, model, user, app, max_tokens, price)
    try:
        result, usage = _normalize(fn())
    except BaseException as exc:
        _on_fail(provider, now, exc)
        raise
    _after(provider, model, now, price, ledger_user, usage)
    if cache is not None and cache_key is not None:
        cache.set(cache_key, result)
    return result


async def acall(
    provider: str,
    model: str,
    fn: Callable[[], Awaitable[tuple]],
    *,
    user: Optional[str] = None,
    app: Optional[str] = None,
    max_tokens: Optional[int] = None,
    price: Optional[tuple[float, float]] = None,
) -> Any:
    """Async-вход (forecast-bot): `fn` — корутина-фабрика, возвращает `(result, tin, tout)`
    или `(result, TokenUsage)`. Те же проверки, что у `call()`, в том же порядке."""
    now, price, ledger_user = _gate(provider, model, user, app, max_tokens, price)
    try:
        result, usage = _normalize(await fn())
    except BaseException as exc:
        _on_fail(provider, now, exc)
        raise
    _after(provider, model, now, price, ledger_user, usage)
    return result


def spent_today(provider: Optional[str] = None) -> float:
    conn = _conn()
    try:
        return round(_sum_cost(conn, _day_start(time.time()), provider), 6)
    finally:
        conn.close()


def spent_today_app(app: str) -> float:
    conn = _conn()
    try:
        return round(_app_cost_today(conn, app, _day_start(time.time())), 6)
    finally:
        conn.close()


def spent_by_user(user: str, since: float) -> tuple[float, int]:
    """(сумма $, число вызовов) по точному полю `user` с момента `since` — для журнала прогнозов."""
    conn = _conn()
    try:
        r = conn.execute(
            'SELECT COALESCE(SUM(cost_usd),0) s, COUNT(*) n FROM usage WHERE "user" = ? AND ts >= ?',
            (user, since),
        ).fetchone()
        return round(float(r["s"]), 6), int(r["n"])
    finally:
        conn.close()


def app_cost_since(app: str, since: float) -> float:
    """Сколько $ приложение потратило с момента `since` (лимит одного запуска бота)."""
    conn = _conn()
    try:
        return float(conn.execute(
            'SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE "user" LIKE ? AND ts >= ?', (f"{app}:%", since)
        ).fetchone()["s"])
    finally:
        conn.close()


def count_app_calls(app: str, since: float) -> int:
    conn = _conn()
    try:
        return int(conn.execute(
            'SELECT COUNT(*) n FROM usage WHERE "user" LIKE ? AND ts >= ?', (f"{app}:%", since)
        ).fetchone()["n"])
    finally:
        conn.close()
