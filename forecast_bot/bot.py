"""Наш бот = подкласс шаблонного `FallTemplateBot2026` (vendor/metac_bot_template/main.py).

Отличия от шаблона:
- все четыре роли LLM — `GuardedLlm` (через ai_guard), дефолты шаблона по env не используются;
- `publish_reports_to_metaculus=False` ВСЕГДА: отправку делает раннер после своих проверок
  (журнал, лимиты, сторож гарда) — одна точка, которую видно и которую можно протестировать;
- счётчик вызовов AskNews на вопрос (квота 1k/мес у бесплатного доступа Metaculus).
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from forecast_bot.guarded_llm import GuardedLlm, install_sentinel

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "vendor" / "metac_bot_template"

ASKNEWS_PRESET = "asknews/news-summaries"
# «свежие новости» = 1 вызов, архив «news knowledge» = 5 (страница ресурсов Metaculus 38928,
# раздел «Getting AskNews Setup»); пресет news-summaries делает оба запроса
# (forecasting_tools/helpers/asknews_searcher.py:get_formatted_news_async).
ASKNEWS_CALLS_PER_RESEARCH = 6

DEFAULT_MODEL = "openrouter/anthropic/claude-opus-5.5"
DEFAULT_PARSER = "openrouter/openai/gpt-4o-mini"  # дефолт шаблона для OpenRouter (forecast_bot.py:1013)


def _load_template_module():
    """Импорт шаблона без его `dotenv.load_dotenv()`: тот ищет .env вверх по дереву от main.py
    и подтянул бы боевые ключи в тесты. Env грузит раннер явно — из `paths.env_path()`."""
    if "metac_template_main" in sys.modules:
        return sys.modules["metac_template_main"]
    import dotenv

    sys.path.insert(0, str(TEMPLATE_DIR))
    original = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: False  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location("metac_template_main", TEMPLATE_DIR / "main.py")
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules["metac_template_main"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        dotenv.load_dotenv = original  # type: ignore[assignment]
    return module


_template = _load_template_module()
FallTemplateBot2026 = _template.FallTemplateBot2026


def build_llms() -> dict[str, Any]:
    model = os.environ.get("FORECAST_MODEL", DEFAULT_MODEL)
    parser = os.environ.get("FORECAST_PARSER_MODEL", DEFAULT_PARSER)
    # max_tokens держит pre-check ai_guard per-call $0.50: 16000 × $0.020/1k = $0.32 для Opus 5.5.
    return {
        "default": GuardedLlm(model=model, temperature=0.3, timeout=180, allowed_tries=2,
                              max_tokens=int(os.environ.get("FORECAST_MAX_TOKENS", "16000"))),
        "summarizer": GuardedLlm(model=parser, temperature=0.3, timeout=60, allowed_tries=2, max_tokens=2000),
        "researcher": ASKNEWS_PRESET,
        "parser": GuardedLlm(model=parser, temperature=0.3, timeout=60, allowed_tries=2, max_tokens=2000),
    }


class ForecastBot(FallTemplateBot2026):
    def __init__(self, **kwargs: Any) -> None:
        install_sentinel()
        kwargs.setdefault("research_reports_per_question", 1)
        kwargs.setdefault("predictions_per_research_report", int(os.environ.get("FORECAST_PREDICTIONS", "5")))
        kwargs.setdefault("use_research_summary_to_forecast", False)
        kwargs.setdefault("folder_to_save_reports_to", None)
        kwargs.setdefault("extra_metadata_in_explanation", True)
        kwargs.setdefault("llms", build_llms())
        # Отправка и пропуск уже спрогнозированных — забота раннера, не шаблона.
        kwargs["publish_reports_to_metaculus"] = False
        kwargs["skip_previously_forecasted_questions"] = False
        super().__init__(**kwargs)
        self.asknews_calls: Counter[int] = Counter()
        # У шаблона семафор — атрибут класса, привязывается к первому event loop; второй
        # asyncio.run в том же процессе (тесты) упал бы «bound to a different event loop».
        self._concurrency_limiter = asyncio.Semaphore(self._max_concurrent_questions)

    async def run_research(self, question: Any) -> str:
        if self.get_llm("researcher") == ASKNEWS_PRESET:
            # Считаем ДО вызова: квота тратится и тогда, когда ответ потом упал.
            self.asknews_calls[question.id_of_question] += ASKNEWS_CALLS_PER_RESEARCH
        return await super().run_research(question)
