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
from collections import Counter
from typing import Any, Callable

from forecasting_tools import GeneralLlm
from forecasting_tools.ai_models import general_llm as _gl

from forecast_bot import ai_guard
from forecast_bot.ai_guard import AIGuardError, BudgetExceeded, TokenUsage

APP = "forecast"

# Кому списывать вызов в леджере: "q<id вопроса>" (ставит раннер на время вопроса).
CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar("forecast_user", default="run")
_IN_GUARD: contextvars.ContextVar[bool] = contextvars.ContextVar("forecast_in_guard", default=False)

# Учёт для раннера: сколько вызовов прошло через гард и кто упёрся в лимит.
GUARDED_CALLS: Counter[str] = Counter()
BUDGET_HITS: dict[str, str] = {}
UNGUARDED_ATTEMPTS: list[str] = []

# Пауза между повторами при сетевых ошибках (тесты ставят 0).
RETRY_BACKOFF_S = [5.0, 15.0, 30.0]
# Упёрлись в RPM/TPM гарда (окно 60 с, общее на машину) — ждём окно, не бросаем вопрос.
RATE_WAIT_S = 61.0
RATE_WAIT_TRIES = 5


class UnguardedLlmCall(RuntimeError):
    """LLM позвали мимо ai_guard — вызов отклонён до сети."""


def _is_rate_limit(exc: BudgetExceeded) -> bool:
    msg = str(exc)
    return msg.startswith("RPM ") or msg.startswith("TPM ")


def provider_for(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else "openai"


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
            token = _IN_GUARD.set(True)
            try:
                resp = await parent(prompt)
            finally:
                _IN_GUARD.reset(token)
            usage = TokenUsage(
                input=int(resp.prompt_tokens_used or 0),
                output=int(resp.completion_tokens_used or 0),
                actual_cost_usd=float(resp.cost) if resp.cost else None,
            )
            return resp, usage

        result = await ai_guard.acall(
            provider_for(self.model), self.model, fn,
            user=user, app=APP, max_tokens=self.guard_max_tokens,
        )
        GUARDED_CALLS[user] += 1
        return result


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
        return await _BACKEND[name](*args, **kwargs)
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
