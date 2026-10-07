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

from forecast_bot.ai_guard import BudgetExceeded
from forecast_bot.guarded_llm import GuardedLlm, UnguardedLlmCall, install_sentinel

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "vendor" / "metac_bot_template"

ASKNEWS_PRESET = "asknews/news-summaries"
# «свежие новости» = 1 вызов, архив «news knowledge» = 5 (страница ресурсов Metaculus 38928,
# раздел «Getting AskNews Setup»); пресет news-summaries делает оба запроса
# (forecasting_tools/helpers/asknews_searcher.py:get_formatted_news_async).
ASKNEWS_CALLS_PER_RESEARCH = 6
# Только «свежие новости» (48 ч) — 1 вызов на вопрос. Указание income 02.10.2026: турнирный
# бесплатный лимит не подтверждён, расходовать экономно (≤1–2 вызова на вопрос).
ASKNEWS_LATEST = "asknews/latest-news"
# Агентный исследователь (блок 2.0): до MAX_NEWS_CALLS свежих поисков AskNews на вопрос.
AGENT_RESEARCHER = "agent"
ASKNEWS_CALLS = {ASKNEWS_PRESET: ASKNEWS_CALLS_PER_RESEARCH, ASKNEWS_LATEST: 1}

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


RESEARCH_MODES = ("asknews", "asknews-latest", "online", "none", "agent")


def build_researcher(mode: str, model: str) -> Any:
    """asknews — пресет шаблона (бесплатная квота Metaculus); online — та же модель с веб-поиском
    OpenRouter `:online` (нативный поиск провайдера, по странице ресурсов Metaculus — покрывается
    кредитами, если не Exa); none — без поиска (шаблонная ветка `no_research`)."""
    if mode == "asknews":
        return ASKNEWS_PRESET
    if mode == "asknews-latest":
        return ASKNEWS_LATEST
    if mode == "online":
        return GuardedLlm(model=f"{model}:online", temperature=0.1, timeout=180, allowed_tries=2, max_tokens=4000)
    if mode == "none":
        return "no_research"
    if mode == "agent":
        return AGENT_RESEARCHER
    raise ValueError(f"неизвестный режим поиска: {mode} (есть {RESEARCH_MODES})")


def build_llms() -> dict[str, Any]:
    model = os.environ.get("FORECAST_MODEL", DEFAULT_MODEL)
    parser = os.environ.get("FORECAST_PARSER_MODEL", DEFAULT_PARSER)
    research = os.environ.get("FORECAST_RESEARCH", "asknews")
    # max_tokens держит pre-check ai_guard per-call $0.50: 16000 × $0.020/1k = $0.32 для Opus 5.5.
    reasoning = os.environ.get("FORECAST_REASONING", "").strip()  # low|medium|high → reasoning_effort
    # С reasoning у Claude температура допустима только по умолчанию — не передаём её.
    extra = {"reasoning_effort": reasoning} if reasoning else {}
    return {
        "default": GuardedLlm(model=model, temperature=None if reasoning else 0.3, timeout=300, allowed_tries=2,
                              max_tokens=int(os.environ.get("FORECAST_MAX_TOKENS", "16000")), **extra),
        "summarizer": GuardedLlm(model=parser, temperature=0.3, timeout=60, allowed_tries=2, max_tokens=2000),
        "researcher": build_researcher(research, model),
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
        self.web_searches: Counter[int] = Counter()  # веб-поиски агента (OpenRouter + Exa) на вопрос
        self.research_stats: dict[int, dict] = {}  # сверка чисел исследования агента (verify.check)
        self.quant_hints: dict[int, str] = {}      # подсказки quant, ушедшие прогнозисту (Market Pulse)
        self.prediction_sets: dict[int, list] = {}  # все прогнозы вопроса ДО агрегации (калибровка, этап 4)
        # У шаблона семафор — атрибут класса, привязывается к первому event loop; второй
        # asyncio.run в том же процессе (тесты) упал бы «bound to a different event loop».
        self._concurrency_limiter = asyncio.Semaphore(self._max_concurrent_questions)

    async def _aggregate_predictions(self, predictions: list, question: Any) -> Any:
        """Все прогнозы вопроса сохраняются до агрегации (медиана шаблона не меняется) — разброс для калибровки."""
        self.prediction_sets[question.id_of_question] = list(predictions)
        return await super()._aggregate_predictions(predictions, question)

    @staticmethod
    def search_backend() -> str:
        """Поиск агента: web (OpenRouter + Exa, по умолчанию с 05.10.2026) | asknews (выключен) | none."""
        return os.environ.get("FORECAST_SEARCH", "web").strip() or "web"

    @property
    def agent_max_searches(self) -> int:
        from forecast_bot.agent import MAX_NEWS_CALLS

        return int(os.environ.get("FORECAST_AGENT_MAX_NEWS", MAX_NEWS_CALLS))

    @property
    def asknews_calls_per_research(self) -> int:
        """Сколько вызовов AskNews может уйти на вопрос — для месячного потолка AskNews."""
        researcher = self.get_llm("researcher")
        if researcher == AGENT_RESEARCHER:
            return self.agent_max_searches if self.search_backend() == "asknews" else 0
        return ASKNEWS_CALLS.get(researcher, 0) if isinstance(researcher, str) else 0

    async def _multiple_choice_prompt_to_forecast(self, question: Any, prompt: str) -> Any:
        """MC: явные названия вариантов в промпте + детерминированный разбор ответа (forecast_bot/mc.py).
        LLM-парсер шаблона — только если ответ не разобрался однозначно."""
        from forecasting_tools import PredictedOptionList, ReasonedPrediction, clean_indents, structure_output
        from forecasting_tools.data_models.multiple_choice_report import PredictedOption

        from forecast_bot import mc

        reasoning = await self.get_llm("default", "llm").invoke(mc.patch_prompt(prompt, list(question.options)))
        parsed = mc.parse_final(reasoning, list(question.options))
        if parsed is not None:
            options = PredictedOptionList(predicted_options=[
                PredictedOption(option_name=o, probability=parsed[o]) for o in question.options])
            return ReasonedPrediction(prediction_value=options, reasoning=reasoning)
        parsing_instructions = clean_indents(f"""
            Make sure that all option names are one of the following:
            {question.options}
            If the text uses letters (Option_A, Option_B, ...), Option_A is the first option in this list, Option_B the
            second, and so on. Do not skip options with 0%; include them with 0% probability.
            {self._create_resolved_question_parsing_message()}
            """)
        options = await structure_output(text_to_structure=reasoning, output_type=PredictedOptionList,
                                         model=self.get_llm("parser", "llm"),
                                         num_validation_samples=self._structure_output_validation_samples,
                                         additional_instructions=parsing_instructions)
        return ReasonedPrediction(prediction_value=options, reasoning=reasoning)

    async def run_research(self, question: Any) -> str:
        hint = await asyncio.to_thread(self.quant_hint, question)
        if hint:
            # Вопрос по рыночному ряду (Market Pulse): база по самому ряду, без поиска — подвопросы обновляются
            # до ~11 раз, и поиск на каждое обновление съел бы квоту AskNews (900/мес). Проверено сухим
            # прогоном 26Q3: tools/pulse_dryrun.py (поиск выключен, подсказка quant).
            self.quant_hints[question.id_of_question] = hint
            return hint + "\n\nNo news search was run for this market-series question."
        return await self._run_research_inner(question)

    def quant_hint(self, question: Any) -> str | None:
        """Статистическая база по ряду (Market Pulse) — только при FORECAST_QUANT_HINTS=1.
        Числа посчитаны нами по данным строго до сегодняшней даты (FORECAST_ASOF — для сухих прогонов)."""
        if os.environ.get("FORECAST_QUANT_HINTS", "").strip() != "1":
            return None
        from datetime import date, datetime, timezone

        from forecast_bot import paths, quant

        label = getattr(question, "group_question_option", None)
        title = (getattr(question, "api_json", None) or {}).get("title") or question.question_text
        asof_env = os.environ.get("FORECAST_ASOF", "").strip()
        asof = date.fromisoformat(asof_env) if asof_env else datetime.now(timezone.utc).date()
        year = (question.close_time or datetime.now(timezone.utc)).year
        try:
            q = quant.pulse_quant(title, label or "", year, asof, paths.data_dir() / "polygon" / "series")
        except Exception:  # ряд недоступен — без подсказки, прогноз идёт как обычно
            return None
        return q.hint() if q else None

    async def _run_research_inner(self, question: Any) -> str:
        researcher = self.get_llm("researcher")
        if isinstance(researcher, str) and researcher in ASKNEWS_CALLS:
            # Считаем ДО вызова: квота тратится и тогда, когда ответ потом упал.
            self.asknews_calls[question.id_of_question] += ASKNEWS_CALLS[researcher]
        if researcher == AGENT_RESEARCHER:
            return await self._agent_research(question)
        try:
            if researcher == ASKNEWS_LATEST:
                async with self._concurrency_limiter:
                    return await self._asknews_latest(question.question_text)
            return await super().run_research(question)
        except (BudgetExceeded, UnguardedLlmCall):
            raise  # отказ гарда — честный пропуск вопроса, не «исследование без новостей»
        except Exception as exc:
            # Поиск упал (05.10.2026: кошелёк AskNews пуст, 402001) — прогноз без новостей лучше, чем никакого.
            return f"News search was unavailable ({type(exc).__name__}); forecast from the question text alone."

    async def _agent_research(self, question: Any) -> str:
        from forecast_bot.agent import ResearchAgent

        backend = self.search_backend()
        news = {"web": self._web_search, "asknews": self._asknews_latest}.get(backend)
        agent = ResearchAgent(
            os.environ.get("FORECAST_AGENT_MODEL", os.environ.get("FORECAST_MODEL", DEFAULT_MODEL)),
            question_budget_usd=float(os.environ.get("FORECAST_QUESTION_BUDGET", "0.30")),
            news=news,
            max_news=self.agent_max_searches,
        )
        async with self._concurrency_limiter:
            try:
                return await agent.research(question)
            finally:
                counter = self.web_searches if backend == "web" else self.asknews_calls
                counter[question.id_of_question] += agent.news_calls
                if agent.verdict is not None:
                    from forecast_bot import calib

                    self.research_stats[question.id_of_question] = {
                        "research_numbers": agent.verdict.numbers_total,
                        "research_unverified": agent.verdict.numbers_unverified,
                        "research_dropped": "\n".join(agent.verdict.dropped)[:4000],
                        **calib.research_strength(agent.verdict.facts_cited, agent.verdict.cited_sources,
                                                  agent.source_meta),
                    }

    async def _web_search(self, query: str) -> str:
        from forecast_bot import websearch

        return await websearch.search(query)

    async def _asknews_latest(self, query: str) -> str:
        """Один запрос AskNews «latest news» (48 ч) — та же разметка, что у пресета шаблона
        (forecasting_tools/helpers/asknews_searcher.py:get_formatted_news_async), без архива."""
        from forecast_bot import guarded_llm

        if guarded_llm.APP != "forecast":
            # Урок 05.10.2026: замеры и бэктесты съели боевой кошелёк AskNews (231 вызов по журналу) — вне боя нельзя.
            raise RuntimeError(f"AskNews запрещён вне боя (приложение {guarded_llm.APP!r})")
        from asknews_sdk import AsyncAskNewsSDK
        from forecasting_tools import AskNewsSearcher

        searcher = AskNewsSearcher()  # берёт ключи из env и валидирует их
        async with AsyncAskNewsSDK(client_id=searcher.client_id, client_secret=searcher.client_secret,
                                   api_key=searcher.api_key, scopes={"news"}) as ask:
            response = await ask.news.search_news(query=query, n_articles=8, return_type="both",
                                                  strategy="latest news")
        articles = response.as_dicts
        if not articles:
            return "Here are the relevant news articles:\n\nNo articles were found.\n"
        return "Here are the relevant news articles:\n\n" + searcher._format_articles(articles)
