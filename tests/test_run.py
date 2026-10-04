"""Раннер на фейковом клиенте: типы вопросов, формат отправки, dry, «не дважды», лимит гарда."""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fakes import FakeMetaculusClient, questions

from forecast_bot import ai_guard, run as R
from forecast_bot.journal import Journal


def _run(client, *, submit, limit=None, bot=None, journal=None, **kw):
    from forecast_bot.bot import ForecastBot
    from forecast_bot import paths

    bot = bot or ForecastBot()
    journal = journal or Journal(paths.journal_db())
    return asyncio.run(R.run(client=client, bot=bot, journal=journal, tournaments=["minibench"],
                             submit=submit, limit=limit, **kw)), journal


def _ledger_rows():
    conn = sqlite3.connect(ai_guard._ledger_path())
    try:
        return conn.execute('SELECT "user", cost_usd FROM usage').fetchall()
    finally:
        conn.close()


def test_all_four_types_produce_valid_payload_and_private_comment(fake_llm, fake_asknews):
    client = FakeMetaculusClient(questions())
    result, _ = _run(client, submit=True)

    assert [r["status"] for r in result.rows] == ["ok"] * 4
    posted = dict(client.predictions)
    assert posted[201] == {"probability_yes": pytest.approx(0.3)}
    for qid, size in ((202, 201), (203, 12)):  # numeric: 200+1 точек CDF; discrete: 11 исходов + 1
        cdf = posted[qid]["continuous_cdf"]
        assert len(cdf) == size and all(a <= b for a, b in zip(cdf, cdf[1:]))
    mc = posted[204]["probability_yes_per_category"]
    assert set(mc) == {"Альфа", "Бета", "Гамма"} and sum(mc.values()) == pytest.approx(1.0, abs=1e-3)

    assert [c["post_id"] for c in client.comments] == [101, 102, 103, 104]
    assert all(c["is_private"] and "Рассуждение" in c["text"] for c in client.comments)


def test_dry_by_default_posts_nothing_but_journals_reasoning(fake_llm, fake_asknews):
    client = FakeMetaculusClient(questions())
    result, journal = _run(client, submit=False, limit=2)

    assert client.predictions == [] and client.comments == []
    rows = journal.rows(result.run_id)
    assert [(r["mode"], r["status"]) for r in rows] == [("dry", "ok")] * 2
    assert all(r["reasoning"] and r["cost_usd"] > 0 and r["asknews_calls"] == 6 for r in rows)


def test_submit_requires_env_flag_too():
    assert R.submit_allowed("submit", {"FORECAST_SUBMIT": "1"}) is True
    assert R.submit_allowed("submit", {}) is False
    assert R.submit_allowed("submit", {"FORECAST_SUBMIT": "yes"}) is False
    assert R.submit_allowed("dry", {"FORECAST_SUBMIT": "1"}) is False


def test_main_submit_without_flag_refuses_before_any_work(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(R, "run", lambda **k: called.append(k))
    assert R.main(["--mode", "submit"]) == 3
    assert called == [] and "FORECAST_SUBMIT=1" in capsys.readouterr().out


def test_main_without_keys_refuses(monkeypatch):
    monkeypatch.delenv("ASKNEWS_API_KEY")
    assert R.main(["--mode", "dry"]) == 2


def test_not_twice_second_run_posts_nothing(fake_llm, fake_asknews):
    client = FakeMetaculusClient(questions())
    _, journal = _run(client, submit=True)
    n = len(client.predictions)
    calls_before = len(fake_llm.calls)
    result, _ = _run(client, submit=True, journal=journal)
    assert len(client.predictions) == n == 4
    assert result.rows == [] and len(fake_llm.calls) == calls_before


def test_already_forecasted_on_metaculus_is_skipped(fake_llm, fake_asknews):
    client = FakeMetaculusClient(questions(forecasted_ids=(201, 202, 203)))
    _run(client, submit=True)
    assert [qid for qid, _ in client.predictions] == [204]


def test_dry_row_does_not_block_real_submit(fake_llm, fake_asknews):
    client = FakeMetaculusClient(questions()[:1])
    _, journal = _run(client, submit=False)
    assert client.predictions == []
    _run(client, submit=True, journal=journal)
    assert [qid for qid, _ in client.predictions] == [201]


def test_budget_exhausted_is_honest_skip_not_crash(fake_llm, fake_asknews, monkeypatch):
    monkeypatch.setitem(ai_guard.APP_LIMITS, "forecast", {"day_usd": 0.01})
    conn = ai_guard._conn()
    conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)', (time.time(), "openrouter", "m", "forecast:old", 0, 0, 0.02))
    conn.commit(); conn.close()

    client = FakeMetaculusClient(questions())
    result, journal = _run(client, submit=True)
    assert client.predictions == [] and client.comments == []
    assert [r["status"] for r in journal.rows(result.run_id)] == ["skipped_budget"]
    assert "forecast" in result.stopped_reason and fake_llm.calls == []


def test_budget_hit_mid_question_publishes_nothing(fake_llm, fake_asknews, monkeypatch):
    # Хватает на несколько вызовов: часть прогнозов успеет, но отправлять половину нельзя.
    # Фейк: 1000 вход + 200 выход Opus 5.5 = $0.008 за вызов.
    monkeypatch.setitem(ai_guard.APP_LIMITS, "forecast", {"day_usd": 0.02})
    client = FakeMetaculusClient(questions())
    result, journal = _run(client, submit=True)
    assert client.predictions == []
    assert [r["status"] for r in journal.rows(result.run_id)] == ["skipped_budget"]
    assert 0 < len(fake_llm.calls) < 16


def test_run_budget_stops_run_and_publishes_nothing_partial(fake_llm, fake_asknews):
    """Лимит одного запуска ($1 в Actions): первый вопрос уложился, второй упёрся — стоп, без отправки."""
    from forecast_bot import guarded_llm

    # Фейк: Opus 5.5 $0.008 за вызов, ~16 вызовов на вопрос ≈ $0.06 на вопрос.
    guarded_llm.start_run(0.08)
    client = FakeMetaculusClient(questions())
    result, _ = _run(client, submit=True)
    assert [r["status"] for r in result.rows] == ["ok", "skipped_budget"]
    assert [qid for qid, _ in client.predictions] == [201]
    assert "лимит запуска" in result.stopped_reason


def test_daily_cap_counts_earlier_runs_from_saved_ledger(fake_llm, fake_asknews):
    """$6/сутки (с 03.10.2026) — по леджеру, который переживает запуски (в Actions — кэш state/)."""
    conn = ai_guard._conn()
    conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                 (time.time() - 60, "openrouter", "m", "forecast:q1", 0, 0, 6.0))
    conn.commit(); conn.close()
    client = FakeMetaculusClient(questions())
    result, _ = _run(client, submit=True)
    assert [r["status"] for r in result.rows] == ["skipped_budget"] and client.predictions == []
    assert "дневной лимит $6.0" in result.stopped_reason


def test_summary_lists_run_without_secrets(fake_llm, fake_asknews, capsys):
    from forecast_bot import summary

    client = FakeMetaculusClient(questions()[:2])
    _run(client, submit=True)
    out = summary.render(time.time() - 600)
    assert "отправлено: 2" in out and "Будет ли X?" in out
    for secret in ("fake-metaculus", "fake-openrouter", "fake-asknews"):
        assert secret not in out


def test_workflow_gates_and_limits():
    """Workflow (блок 2.2): цикл в одном job, вариант B, перезапуск себя, cron-страховка раз в час, без наслоения."""
    from pathlib import Path

    import yaml

    wf = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github/workflows/forecast.yml").read_text())
    job = wf["jobs"]["forecast"]
    assert job["if"] == "vars.FORECAST_SUBMIT == '1'" and job["timeout-minutes"] == 350
    assert wf["concurrency"] == {"group": "forecast", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "read", "actions": "write"}
    on = wf[True] if True in wf else wf["on"]  # YAML 1.1 читает ключ on как True
    assert on["schedule"] == [{"cron": "17 * * * *"}] and "workflow_dispatch" in on
    steps = {s.get("name"): s for s in job["steps"]}
    cmd = steps["Цикл прогнозов и отправки"]["run"]
    for part in ("--mode submit", "--tournaments \"${{ vars.FORECAST_TOURNAMENTS || 'fall,minibench' }}\"",
                 "--quant-hints", "--research agent",
                 "--agent-model openrouter/anthropic/claude-haiku-4.5", "--agent-max-news 2",
                 "--model openrouter/anthropic/claude-opus-5.5", "--reasoning high", "--predictions 1",
                 "--run-budget 1.0", "--loop-minutes 335", "--poll-minutes 10"):
        assert part in cmd
    # окно цикла + запас на последний вопрос + подготовка job укладываются в timeout
    assert 335 + R.LOOP_GRACE_S / 60 + 2 + 3 < job["timeout-minutes"] <= 360
    restart = steps["Перезапустить цикл"]
    assert restart["if"] == "success() || failure()" and "gh workflow run forecast.yml" in restart["run"]
    assert restart["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    names = [s.get("name") for s in job["steps"]]
    assert names.index("Сохранить состояние") < names.index("Перезапустить цикл")
    assert all(v.startswith("${{ secrets.") for k, v in job["env"].items() if k.endswith(("_TOKEN", "_KEY")))


def test_every_llm_call_lands_in_ledger(fake_llm, fake_asknews):
    client = FakeMetaculusClient(questions()[:2])
    result, _ = _run(client, submit=False)
    rows = _ledger_rows()
    assert len(rows) == len(fake_llm.calls) > 0
    assert {u for u, _ in rows} == {"forecast:q201", "forecast:q202"}
    assert sum(r["llm_calls"] for r in result.rows) == len(rows)


def test_ledger_miss_blocks_submission(fake_llm, fake_asknews, monkeypatch):
    """Сторож: вызов прошёл, а строки в леджере нет — отправлять нельзя."""
    real = ai_guard._record
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] != 2:
            real(*a, **k)

    monkeypatch.setattr(ai_guard, "_record", flaky)
    client = FakeMetaculusClient(questions()[:1])
    result, _ = _run(client, submit=True)
    assert client.predictions == []
    assert result.rows[0]["status"] == "error" and "сторож" in result.rows[0]["error"]


def test_asknews_monthly_cap_skips_without_llm(fake_llm, fake_asknews):
    from forecast_bot import paths

    journal = Journal(paths.journal_db())
    journal.record(run_id="old", question_id=1, mode="dry", status="ok", asknews_calls=R.ASKNEWS_MONTHLY_CAP - 5)
    client = FakeMetaculusClient(questions())
    result, _ = _run(client, submit=True, journal=journal)
    assert [r["status"] for r in result.rows] == ["skipped_asknews_quota"]
    assert fake_llm.calls == [] and fake_asknews == [] and client.predictions == []


def test_cli_model_and_predictions_reach_bot(monkeypatch):
    seen = {}

    async def fake_run(**k):
        seen["model"] = k["bot"].get_llm("default", "llm").model
        seen["n"] = k["bot"].predictions_per_research_report
        return R.RunResult(run_id="t", submit=False)

    monkeypatch.setattr(R, "run", fake_run)
    assert R.main(["--model", "openrouter/google/gemini-3.5-flash", "--predictions", "1"]) == 0
    assert seen == {"model": "openrouter/google/gemini-3.5-flash", "n": 1}


def test_cli_variant_b_flags(monkeypatch):
    """Вариант B одной строкой: агент на Haiku, итог Opus high, ≤2 поиска AskNews."""
    import os

    seen = {}

    async def fake_run(**k):
        bot = k["bot"]
        seen.update(model=bot.get_llm("default", "llm").model, research=bot.get_llm("researcher"),
                    news=bot.asknews_calls_per_research, agent=os.environ.get("FORECAST_AGENT_MODEL"),
                    effort=bot.get_llm("default", "llm").litellm_kwargs.get("reasoning_effort"))
        return R.RunResult(run_id="t", submit=False)

    monkeypatch.setattr(R, "run", fake_run)
    for k in ("FORECAST_AGENT_MODEL", "FORECAST_AGENT_MAX_NEWS", "FORECAST_REASONING", "FORECAST_RESEARCH"):
        monkeypatch.delenv(k, raising=False)
    assert R.main(["--research", "agent", "--agent-model", "openrouter/anthropic/claude-haiku-4.5",
                   "--agent-max-news", "2", "--model", "openrouter/anthropic/claude-opus-5.5",
                   "--reasoning", "high", "--predictions", "1"]) == 0
    assert seen == {"model": "openrouter/anthropic/claude-opus-5.5", "research": "agent", "news": 2,
                    "agent": "openrouter/anthropic/claude-haiku-4.5", "effort": "high"}


def test_every_default_model_has_a_price():
    from forecast_bot import bot

    for m in (bot.DEFAULT_MODEL, bot.DEFAULT_PARSER, "openrouter/google/gemini-3.5-flash"):
        assert ("openrouter", m) in ai_guard.PRICES


def test_tournament_pins_match_tz():
    from forecasting_tools import MetaculusClient

    assert R.TOURNAMENTS == {"fall": 33121, "minibench": "minibench"}
    assert MetaculusClient.CURRENT_AI_COMPETITION_ID == 33121


@pytest.mark.parametrize("mode,expect_research_call", [("online", True), ("none", False)])
def test_research_modes_without_asknews(fake_llm, fake_asknews, monkeypatch, mode, expect_research_call):
    from forecast_bot.bot import ForecastBot

    monkeypatch.setenv("FORECAST_RESEARCH", mode)
    monkeypatch.setenv("FORECAST_MODEL", "openrouter/google/gemini-3.5-flash")
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    client = FakeMetaculusClient(questions()[:1])
    result, _ = _run(client, submit=False, bot=ForecastBot())
    row, = result.rows
    assert row["status"] == "ok" and row["asknews_calls"] == 0 and fake_asknews == []
    online = [m for m in fake_llm.calls if m.endswith(":online")]
    assert bool(online) is expect_research_call
    news_in_forecast_prompt = any("событие ещё не произошло" in p for p in fake_llm.prompts
                                  if '"Probability: ZZ%"' in p)
    assert news_in_forecast_prompt is expect_research_call


def test_asknews_latest_mode_costs_one_call(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot.bot import ForecastBot

    seen = []

    async def fake_latest(self, query):
        seen.append(query)
        return "Here are the relevant news articles:\n\n**Свежая новость**\nсобытие ещё не произошло"

    monkeypatch.setattr(ForecastBot, "_asknews_latest", fake_latest)
    monkeypatch.setenv("FORECAST_RESEARCH", "asknews-latest")
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    client = FakeMetaculusClient(questions()[:2])
    result, journal = _run(client, submit=False, bot=ForecastBot())
    assert [r["asknews_calls"] for r in result.rows] == [1, 1]
    assert len(seen) == 2 and fake_asknews == []  # архивный пресет шаблона не звали
    assert journal.asknews_calls_this_month() == 2


def test_test_tournament_is_dry_only(monkeypatch):
    monkeypatch.setenv("FORECAST_SUBMIT", "1")
    called = []
    monkeypatch.setattr(R, "run", lambda **k: called.append(k))
    assert R.main(["--mode", "submit", "--tournament", "test"]) == 4
    assert called == []


def test_missing_keys_asknews_only_for_asknews_mode():
    base = {"METACULUS_TOKEN": "t", "OPENROUTER_API_KEY": "k"}
    assert R.missing_keys(base) == ["ASKNEWS_API_KEY"]
    assert R.missing_keys({**base, "FORECAST_RESEARCH": "online"}) == []
    assert R.missing_keys({**base, "FORECAST_RESEARCH": "none"}) == []
