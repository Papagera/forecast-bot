"""Мутационная проверка гардов (§7a): ломаем предохранитель в памяти → его тест ОБЯЗАН покраснеть.

    .venv/bin/python tools/mutate_guards.py

Каждая мутация — в отдельном процессе: подмена исходника модуля (replace по строке) или атрибута,
затем pytest по целевому тесту. Зелёный тест на сломанном коде = слепой гард = код выхода 1.
Если строка для подмены не найдена — тоже провал: мутация устарела и ничего не проверяет.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# имя → (модуль, старая строка, новая строка, целевой тест)
SOURCE_MUTATIONS = {
    "гейт отправки без env": (
        "forecast_bot.run", 'return mode == "submit" and env.get("FORECAST_SUBMIT", "").strip() == "1"',
        'return mode == "submit"', "tests/test_run.py::test_submit_requires_env_flag_too"),
    "main шлёт без флага": (
        "forecast_bot.run", 'if args.mode == "submit" and not submit:', "if False:",
        "tests/test_run.py::test_main_submit_without_flag_refuses_before_any_work"),
    "не дважды: журнал": (
        "forecast_bot.journal", "return row is not None", "return False",
        "tests/test_run.py::test_not_twice_second_run_posts_nothing"),
    "не дважды: флаг Metaculus": (
        "forecast_bot.run", "q.already_forecasted or ", "",
        "tests/test_run.py::test_already_forecasted_on_metaculus_is_skipped"),
    "dry-строка как отправка": (
        "forecast_bot.journal", "AND mode = 'submit' ", "",
        "tests/test_run.py::test_dry_row_does_not_block_real_submit"),
    "лимит гарда не ловится": (
        "forecast_bot.run", "budget_hit = guarded_llm.BUDGET_HITS.pop(user, None)", "budget_hit = None",
        "tests/test_run.py::test_budget_hit_mid_question_publishes_nothing"),
    "сторож обхода выключен": (
        "forecast_bot.guarded_llm", "if not _IN_GUARD.get():", "if False:",
        "tests/test_guard.py::test_sentinel_blocks_plain_generalllm"),
    "сверка с леджером выключена": (
        "forecast_bot.run", " or guarded_calls != ledger_calls", "",
        "tests/test_run.py::test_ledger_miss_blocks_submission"),
    "потолок AskNews снят": (
        "forecast_bot.run", "> asknews_cap:", "> 10**9:",
        "tests/test_run.py::test_asknews_monthly_cap_skips_without_llm"),
    "лимит повторяется": (
        # отказ гарда проваливается в общую ветку «сетевая ошибка → повтор»
        "forecast_bot.guarded_llm", "except BudgetExceeded as exc:", "except ZeroDivisionError as exc:",
        "tests/test_guard.py::test_budget_error_is_not_retried"),
    "фактическая цена игнорируется": (
        "forecast_bot.ai_guard", "if usage.actual_cost_usd is not None and usage.actual_cost_usd > 0:", "if False:",
        "tests/test_guard.py::test_actual_cost_from_provider_wins_over_price_table"),
    "счёт OpenRouter игнорируется": (
        "forecast_bot.guarded_llm", "billed = [c for c in sink if c is not None]", "billed = []",
        "tests/test_guard.py::test_provider_billed_cost_goes_to_ledger"),
    "режим поиска не доходит": (
        "forecast_bot.bot", '"researcher": build_researcher(research, model),', '"researcher": ASKNEWS_PRESET,',
        "tests/test_run.py::test_research_modes_without_asknews"),
    "свежие новости считаются как архив": (
        "forecast_bot.bot", "ASKNEWS_LATEST: 1}", "ASKNEWS_LATEST: 0}",
        "tests/test_run.py::test_asknews_latest_mode_costs_one_call"),
    "лимит запуска не проверяется": (
        "forecast_bot.guarded_llm", "        _check_run_budget()\n", "",
        "tests/test_run.py::test_run_budget_stops_run_and_publishes_nothing_partial"),
    "дневной потолок бота снят": (
        "forecast_bot.ai_guard", '"forecast": {"day_usd": 3.0},', '"forecast": {"day_usd": 1e9},',
        "tests/test_run.py::test_daily_cap_counts_earlier_runs_from_saved_ledger"),
    "агент: новости без лимита": (
        "forecast_bot.agent", "if self.news is None or self.news_calls >= MAX_NEWS_CALLS:", "if self.news is None:",
        "tests/test_agent.py::test_news_calls_capped_per_question"),
    "агент: шаги без лимита": (
        "forecast_bot.agent", "last = step == MAX_STEPS or over_budget", "last = over_budget",
        "tests/test_agent.py::test_steps_capped_and_last_step_has_no_tool_use"),
    "агент: нет мягкого стопа по деньгам": (
        "forecast_bot.agent", "over_budget = self._spent(t0) >= SOFT_STOP_SHARE * self.question_budget_usd",
        "over_budget = False", "tests/test_agent.py::test_soft_budget_stop_forces_final_brief"),
    "агент: ходит в локальную сеть": (
        "forecast_bot.agent", "if ip.is_private or ip.is_loopback", "if False and ip.is_private or ip.is_loopback and False",
        "tests/test_agent.py::test_fetch_url_refuses_non_public"),
    "агент: мимо гарда": (
        "forecast_bot.agent", "resp = await guarded_llm.guarded_completion(self.model, messages, max_tokens=self.max_tokens,",
        "from forecasting_tools.ai_models import general_llm as _g; resp = await _g.acompletion(model=self.model, messages=messages, max_tokens=self.max_tokens,",
        "tests/test_agent.py::test_agent_research_reaches_forecaster_and_ledger"),
    "сверка: выдуманное число проходит": (
        "forecast_bot.verify", "        if missing:\n            unverified += missing", "        if False:\n            unverified += missing",
        "tests/test_verify.py::test_invented_number_drops_whole_fact"),
    "сверка: любой источник вместо процитированного": (
        "forecast_bot.verify", 'ids = (cited_ids(line) & set(norm)) | {"S0"}', 'ids = set(norm)',
        "tests/test_verify.py::test_number_from_wrong_source_is_dropped"),
    "сверка: частичное совпадение числа": (
        "forecast_bot.verify", 'rf"(?<![\\d.]){re.escape(num)}(?:0*)(?![\\d])"', 'rf"{re.escape(num)}"',
        "tests/test_verify.py::test_partial_match_is_not_a_match"),
    "агент: сверка не применяется": (
        "forecast_bot.agent", "        return self.verdict.text\n", "        return self.raw_brief\n",
        "tests/test_verify.py::test_agent_hallucination_is_dropped_and_journaled"),
    "приложение без потолка разрешено": (
        "forecast_bot.guarded_llm", "if app not in ai_guard.APP_LIMITS:", "if False:",
        "tests/test_guard.py::test_unknown_app_name_refused"),
    "RPM валит вопрос": (
        "forecast_bot.guarded_llm", "if _is_rate_limit(exc) and rate_waits < RATE_WAIT_TRIES:", "if False:",
        "tests/test_guard.py::test_rpm_waits_for_window_instead_of_failing"),
}

ATTR_MUTATIONS = {
    "данные в рабочем дереве": (
        "from forecast_bot import paths; paths.main_checkout = lambda root=paths.PACKAGE_ROOT: root",
        "tests/test_guard.py::test_paths_point_to_main_checkout"),
    "голый GeneralLlm вместо GuardedLlm": (
        "import forecast_bot.bot as b; from forecasting_tools import GeneralLlm; "
        "b.GuardedLlm = lambda model, max_tokens=None, **k: GeneralLlm(model, **k)",
        "tests/test_run.py::test_every_llm_call_lands_in_ledger"),
}

SRC_PATCH = (
    "import importlib, sys; m = importlib.import_module({mod!r}); src = open(m.__file__).read(); "
    "assert {old!r} in src, 'MUTATION_STALE'; "
    "exec(compile(src.replace({old!r}, {new!r}, 1), m.__file__, 'exec'), m.__dict__)"
)
RUN = ("import sys; sys.path.insert(0, {root!r}); {patch}; import pytest; "
       "sys.exit(pytest.main(['-q', '-x', '-p', 'no:cacheprovider', '-p', 'no:warnings', {target!r}]))")


def _check(name: str, patch: str, target: str) -> bool:
    proc = subprocess.run([sys.executable, "-c", RUN.format(root=str(ROOT), patch=patch, target=target)],
                          cwd=ROOT, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    if "MUTATION_STALE" in out:
        print(f"{name:34s} → строка не найдена — МУТАЦИЯ УСТАРЕЛА")
        return False
    if "no tests ran" in out or "not found" in out and "ERROR" in out:
        print(f"{name:34s} → тест не найден: {target}")
        return False
    red = proc.returncode != 0
    print(f"{name:34s} → {'КРАСНЫЙ — гард работает' if red else 'зелёный — ГАРД СЛЕПОЙ'}")
    return red


def main() -> int:
    ok = True
    for name, (mod, old, new, target) in SOURCE_MUTATIONS.items():
        ok &= _check(name, SRC_PATCH.format(mod=mod, old=old, new=new), target)
    for name, (patch, target) in ATTR_MUTATIONS.items():
        ok &= _check(name, patch, target)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
