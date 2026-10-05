"""Все LLM-вызовы бота — только через ai_guard (§4b).

Две линии:
1. `GuardedLlm` — подкласс `GeneralLlm` из forecasting-tools. Перекрывает
   `_mockable_direct_call_to_model` (`forecasting_tools/ai_models/general_llm.py:278`) — туда сходятся
   все вызовы модели, в т.ч. `structure_output` и сводка исследования. Вызов идёт через
   `ai_guard.acall`: лимиты ДО вызова, учёт фактических токенов и стоимости ПОСЛЕ.
2. Сторож `install_sentinel()` — подменяет `acompletion`/`aresponses` в модуле general_llm обёрткой,
   которая ОТКАЗЫВАЕТ, если вызов пришёл не изнутри гарда. Новая версия библиотеки, где модель
   зовётся мимо `_mockable_direct_call_to_model`, упадёт `UnguardedLlmCall`, а не потратит деньги молча.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
from collections import Counter
from typing import Any, Callable, Optional

from forecasting_tools import GeneralLlm
from forecasting_tools.ai_models import general_llm as _gl

from forecast_bot import ai_guard
from forecast_bot.ai_guard import AIGuardError, BudgetExceeded, TokenUsage

# Приложение в леджере: "forecast" — бой, "forecast-lab" — замеры вариантов (свой суточный потолок).
# Неизвестное имя запрещено: у него не было бы лимита в APP_LIMITS, то есть это обход потолка.
def _resolve_app(name: str | None) -> str:
    app = (name or "").strip() or "forecast"
    if app not in ai_guard.APP_LIMITS:
        raise RuntimeError(f"FORECAST_APP={app!r}: нет потолка в ai_guard.APP_LIMITS — запуск запрещён")
    return app


APP = _resolve_app(os.environ.get("FORECAST_APP"))

# Кому списывать вызов в леджере: "q<id вопроса>" (ставит раннер на время вопроса).
CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar("forecast_user", default="run")
_IN_GUARD: contextvars.ContextVar[bool] = contextvars.ContextVar("forecast_in_guard", default=False)
# Куда сторож складывает фактическую стоимость из ответа провайдера (см. _billed_cost).
_COST_SINK: contextvars.ContextVar[list | None] = contextvars.ContextVar("forecast_cost_sink", default=None)

# Ключ, под которым litellm кладёт `usage.cost` из ответа OpenRouter
# (litellm/llms/openrouter/chat/transformation.py:transform_response; usage.include ставится всегда).
_OPENROUTER_COST_KEY = "llm_provider-x-litellm-response-cost"

# Учёт для раннера: сколько вызовов прошло через гард и кто упёрся в лимит.
GUARDED_CALLS: Counter[str] = Counter()
BUDGET_HITS: dict[str, str] = {}
UNGUARDED_ATTEMPTS: list[str] = []

# Пауза между повторами при сетевых ошибках (тесты ставят 0).
RETRY_BACKOFF_S = [5.0, 15.0, 30.0]
# Упёрлись в RPM/TPM гарда (окно 60 с, общее на машину) — ждём окно, не бросаем вопрос.
RATE_WAIT_S = 61.0
RATE_WAIT_TRIES = 5


# Лимит одного запуска (Actions: $1 за прогон, указание income 02.10.2026). None — без лимита.
# Сутки ($3) держит APP_LIMITS["forecast"] в ai_guard по сохранённому леджеру.
RUN_BUDGET_USD: float | None = None
RUN_STARTED_AT: float = 0.0


def start_run(budget_usd: float | None) -> None:
    global RUN_BUDGET_USD, RUN_STARTED_AT
    import time

    RUN_BUDGET_USD = budget_usd
    RUN_STARTED_AT = time.time()


def _check_run_budget() -> None:
    if RUN_BUDGET_USD is None:
        return
    spent = ai_guard.app_cost_since(APP, RUN_STARTED_AT)
    if spent >= RUN_BUDGET_USD:
        raise BudgetExceeded(f"лимит запуска ${RUN_BUDGET_USD} исчерпан (потрачено ${spent:.4f})")


class UnguardedLlmCall(RuntimeError):
    """LLM позвали мимо ai_guard — вызов отклонён до сети."""


def _is_rate_limit(exc: BudgetExceeded) -> bool:
    msg = str(exc)
    return msg.startswith("RPM ") or msg.startswith("TPM ")


def provider_for(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else "openai"


def price_key(model: str) -> str:
    """`:online` (веб-поиск OpenRouter) не меняет токенную цену; плата за поиск придёт
    в фактической стоимости ответа, а не в pre-check."""
    return model.removesuffix(":online")


def _billed_cost(response: Any) -> float | None:
    hidden = getattr(response, "_hidden_params", None) or {}
    value = (hidden.get("additional_headers") or {}).get(_OPENROUTER_COST_KEY)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class GuardedLlm(GeneralLlm):
    def __init__(self, model: str, *, max_tokens: int | None = None, **kwargs: Any) -> None:
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        super().__init__(model, **kwargs)
        self.guard_max_tokens = max_tokens

    async def _invoke_with_request_cost_time_and_token_limits_and_retry(self, prompt: Any) -> Any:
        """Свой цикл повторов вместо tenacity шаблона: отказ гарда НЕ повторяем —
        лимит не рассосётся за 60 секунд ожидания, а повтор только жжёт время."""
        tries = max(1, int(self.allowed_tries))
        attempt = 0
        rate_waits = 0
        while True:
            try:
                return await self._mockable_direct_call_to_model(prompt)
            except BudgetExceeded as exc:
                # RPM/TPM — это темп, а не бюджет: окно 60 с, ждём и пробуем снова (ограниченно).
                if _is_rate_limit(exc) and rate_waits < RATE_WAIT_TRIES:
                    rate_waits += 1
                    await asyncio.sleep(RATE_WAIT_S)
                    continue
                BUDGET_HITS.setdefault(CURRENT_USER.get(), str(exc))
                raise
            except (AIGuardError, UnguardedLlmCall):
                raise
            except Exception:
                attempt += 1
                if attempt >= tries:
                    raise
                await asyncio.sleep(RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)])

    async def _mockable_direct_call_to_model(self, prompt: Any) -> Any:
        user = CURRENT_USER.get()
        parent = super()._mockable_direct_call_to_model

        async def fn():
            sink: list = []
            token = _IN_GUARD.set(True)
            sink_token = _COST_SINK.set(sink)
            try:
                resp = await parent(prompt)
            finally:
                _COST_SINK.reset(sink_token)
                _IN_GUARD.reset(token)
            billed = [c for c in sink if c is not None]
            # Приоритет: счёт провайдера (вкл. веб-поиск) → оценка litellm → цена из PRICES (в ai_guard).
            actual = sum(billed) if billed else (float(resp.cost) if resp.cost else None)
            usage = TokenUsage(
                input=int(resp.prompt_tokens_used or 0),
                output=int(resp.completion_tokens_used or 0),
                actual_cost_usd=actual,
            )
            return resp, usage

        _check_run_budget()
        provider = provider_for(self.model)
        result = await ai_guard.acall(
            provider, self.model, fn,
            user=user, app=APP, max_tokens=self.guard_max_tokens,
            price=ai_guard.PRICES.get((provider, price_key(self.model))),
        )
        GUARDED_CALLS[user] += 1
        return result


SEARCH_ROW_MODEL = "openrouter/web-search"  # строка леджера для платного поиска (отдельно от токенов модели)


def split_search_cost(usage: dict, billed: Optional[float], price_per_search: float) -> tuple[Optional[float], float, int]:
    """(стоимость модели, стоимость поиска, число поисков) из usage OpenRouter.
    Живьём 05.10.2026: usage.cost 0.010201 = cost_details.upstream_inference_cost 0.003201 + Exa 0.007 —
    счёт OpenRouter уже включает поиск. Есть оба поля → делим по факту; нет → поисков × прайс."""
    n = int(((usage.get("server_tool_use") or {}).get("web_search_requests")) or 1)
    total = usage.get("cost") if usage.get("cost") is not None else billed
    upstream = (usage.get("cost_details") or {}).get("upstream_inference_cost")
    if total is not None and upstream is not None and total >= upstream:
        return float(upstream), float(total) - float(upstream), n
    search = n * price_per_search
    return (float(total) - search if total is not None and total > search else None), search, n


async def guarded_completion(model: str, messages: list[dict], *, max_tokens: int,
                             tries: int = 2, search_price: Optional[float] = None, **kwargs: Any) -> Any:
    """Сырой вызов модели (агент: tools/tool_calls; веб-поиск) — тот же путь, что у GuardedLlm:
    лимит запуска → ai_guard.acall (pre-check, учёт факта) → сторож `acompletion`.
    `search_price` — вызов с веб-поиском OpenRouter: поиск пишется в леджер ОТДЕЛЬНОЙ строкой (SEARCH_ROW_MODEL).
    Возвращает litellm ModelResponse целиком (нужны tool_calls / annotations)."""
    user = CURRENT_USER.get()
    provider = provider_for(model)
    search_cost: list[float] = []

    async def fn():
        sink: list = []
        token = _IN_GUARD.set(True)
        sink_token = _COST_SINK.set(sink)
        try:
            resp = await _gl.acompletion(model=model, messages=messages, max_tokens=max_tokens, **kwargs)
        finally:
            _COST_SINK.reset(sink_token)
            _IN_GUARD.reset(token)
        billed = [c for c in sink if c is not None]
        u = getattr(resp, "usage", None)
        actual = sum(billed) if billed else None
        if search_price is not None:
            udict = u.model_dump() if hasattr(u, "model_dump") else dict(u or {})
            actual, cost_s, _n = split_search_cost(udict, actual, search_price)
            search_cost.append(cost_s)
        usage = TokenUsage(input=int(getattr(u, "prompt_tokens", 0) or 0),
                           output=int(getattr(u, "completion_tokens", 0) or 0),
                           actual_cost_usd=actual)
        return resp, usage

    attempt = rate_waits = 0
    while True:
        try:
            _check_run_budget()
            result = await ai_guard.acall(provider, model, fn, user=user, app=APP, max_tokens=max_tokens,
                                          price=ai_guard.PRICES.get((provider, price_key(model))))
            GUARDED_CALLS[user] += 1
            if search_cost:
                async def search_row():
                    return None, TokenUsage(actual_cost_usd=search_cost[0])

                await ai_guard.acall(provider, SEARCH_ROW_MODEL, search_row, user=user, app=APP, price=(0.0, 0.0))
                GUARDED_CALLS[user] += 1  # строка леджера = учтённый вызов (сверка в run.py)
            return result
        except BudgetExceeded as exc:
            if _is_rate_limit(exc) and rate_waits < RATE_WAIT_TRIES:
                rate_waits += 1
                await asyncio.sleep(RATE_WAIT_S)
                continue
            BUDGET_HITS.setdefault(user, str(exc))
            raise
        except (AIGuardError, UnguardedLlmCall):
            raise
        except Exception:
            attempt += 1
            if attempt >= tries:
                raise
            await asyncio.sleep(RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)])


# ─────────────────────────── сторож ─────────────────────────────────
_BACKEND: dict[str, Callable[..., Any]] = {}


def set_backend(acompletion: Callable[..., Any] | None = None, aresponses: Callable[..., Any] | None = None) -> None:
    """Подменить реальный транспорт (тесты: фейковая модель без сети)."""
    if acompletion is not None:
        _BACKEND["acompletion"] = acompletion
    if aresponses is not None:
        _BACKEND["aresponses"] = aresponses


def _guarded(name: str):
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _IN_GUARD.get():
            UNGUARDED_ATTEMPTS.append(f"{name}:{kwargs.get('model')}")
            raise UnguardedLlmCall(f"{name}({kwargs.get('model')}) вызван мимо ai_guard — отклонено")
        response = await _BACKEND[name](*args, **kwargs)
        sink = _COST_SINK.get()
        if sink is not None:
            sink.append(_billed_cost(response))
        return response
    wrapper.__forecast_sentinel__ = True  # type: ignore[attr-defined]
    return wrapper


def install_sentinel() -> None:
    for name in ("acompletion", "aresponses"):
        current = getattr(_gl, name)
        if getattr(current, "__forecast_sentinel__", False):
            continue
        _BACKEND.setdefault(name, current)
        setattr(_gl, name, _guarded(name))


def sentinel_installed() -> bool:
    return all(getattr(getattr(_gl, n), "__forecast_sentinel__", False) for n in ("acompletion", "aresponses"))


def reset_counters() -> None:
    GUARDED_CALLS.clear()
    BUDGET_HITS.clear()
    UNGUARDED_ATTEMPTS.clear()
