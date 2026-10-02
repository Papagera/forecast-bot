"""ai_guard-обёртка, сторож обхода, пути данных и .gitignore."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from forecast_bot import ai_guard, guarded_llm, paths


def _rows():
    conn = ai_guard._conn()
    try:
        return conn.execute('SELECT provider, model, "user", tokens_in, tokens_out, cost_usd FROM usage').fetchall()
    finally:
        conn.close()


def test_guarded_call_records_actual_tokens_and_cost(fake_llm):
    llm = guarded_llm.GuardedLlm("openrouter/anthropic/claude-opus-5.5", max_tokens=1000)
    token = guarded_llm.CURRENT_USER.set("q7")
    try:
        out = asyncio.run(llm.invoke('"Probability: ZZ%"'))
    finally:
        guarded_llm.CURRENT_USER.reset(token)
    assert "Probability" in out
    (prov, model, user, tin, tout, cost), = _rows()
    assert (prov, user, tin, tout) == ("openrouter", "forecast:q7", 1000, 200)
    assert cost == pytest.approx(1000 / 1000 * 0.004 + 200 / 1000 * 0.020)


def test_actual_cost_from_provider_wins_over_price_table():
    usage = ai_guard.TokenUsage(input=1000, output=200, actual_cost_usd=0.0123)
    assert ai_guard._cost(usage, (0.004, 0.020)) == pytest.approx(0.0123)
    assert ai_guard._cost(ai_guard.TokenUsage(input=1000, output=0), (0.004, 0.020)) == pytest.approx(0.004)


def test_unknown_model_fails_closed(fake_llm):
    llm = guarded_llm.GuardedLlm("openrouter/some/unpriced-model")
    with pytest.raises(ai_guard.AIGuardError):
        asyncio.run(llm.invoke("hi"))
    assert fake_llm.calls == []


def test_sentinel_blocks_plain_generalllm(fake_llm):
    from forecasting_tools import GeneralLlm

    with pytest.raises(guarded_llm.UnguardedLlmCall):
        asyncio.run(GeneralLlm("openrouter/openai/gpt-4o-mini", allowed_tries=1).invoke("hi"))
    assert fake_llm.calls == [] and guarded_llm.UNGUARDED_ATTEMPTS
    assert _rows() == []


def test_per_call_cap_rejects_token_bomb_before_call(fake_llm):
    llm = guarded_llm.GuardedLlm("openrouter/anthropic/claude-opus-5.5", max_tokens=100_000)  # $2 > $0.50
    with pytest.raises(ai_guard.BudgetExceeded):
        asyncio.run(llm.invoke("hi"))
    assert fake_llm.calls == []


def test_kill_switch(fake_llm, monkeypatch):
    monkeypatch.setenv("AI_KILL", "1")
    with pytest.raises(ai_guard.BudgetExceeded):
        asyncio.run(guarded_llm.GuardedLlm("openrouter/openai/gpt-4o-mini").invoke("hi"))
    assert fake_llm.calls == []


def test_budget_error_is_not_retried(fake_llm, monkeypatch):
    calls = []
    real = ai_guard._gate

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(ai_guard, "_gate", counting)
    monkeypatch.setitem(ai_guard.APP_LIMITS, "forecast", {"day_usd": 0.0})
    monkeypatch.setitem(ai_guard.LIMITS, "day_usd", 0.0)
    with pytest.raises(ai_guard.BudgetExceeded):
        asyncio.run(guarded_llm.GuardedLlm("openrouter/openai/gpt-4o-mini", allowed_tries=3).invoke("hi"))
    assert len(calls) == 1


def test_rpm_waits_for_window_instead_of_failing(fake_llm, monkeypatch):
    monkeypatch.setitem(ai_guard.LIMITS, "rpm", 1)
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        if s < guarded_llm.RATE_WAIT_S:  # asyncio.sleep общий: general_llm тоже спит 1e-5 c
            return
        conn = ai_guard._conn()
        conn.execute("UPDATE usage SET ts = ts - 61")  # «прошла минута»
        conn.commit(); conn.close()

    monkeypatch.setattr(guarded_llm.asyncio, "sleep", fake_sleep)
    llm = guarded_llm.GuardedLlm("openrouter/openai/gpt-4o-mini")
    asyncio.run(llm.invoke("a"))
    asyncio.run(llm.invoke("b"))
    assert len(fake_llm.calls) == 2 and slept.count(guarded_llm.RATE_WAIT_S) == 1
    assert guarded_llm.BUDGET_HITS == {}


def test_paths_point_to_main_checkout(monkeypatch):
    assert paths.main_checkout(Path("/x/forecast-bot/.claude/worktrees/stage1")) == Path("/x/forecast-bot")
    assert paths.main_checkout(Path("/x/forecast-bot")) == Path("/x/forecast-bot")
    monkeypatch.delenv("FORECAST_DATA_DIR")
    monkeypatch.delenv("FORECAST_ENV_FILE")
    root = paths.main_checkout()
    assert paths.journal_db() == root / "data" / "journal.db"
    assert paths.env_path() == root / ".env"
    assert ".claude" not in paths.journal_db().parts


def test_env_and_journal_are_gitignored():
    root = Path(__file__).resolve().parent.parent
    for rel in (".env", "data/journal.db", "data/journal.db-wal"):
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root)
        assert r.returncode == 0, f"{rel} не в .gitignore"


def test_load_env_file_does_not_override(tmp_path, monkeypatch):
    from forecast_bot.run import load_env_file

    f = tmp_path / ".env"
    f.write_text("METACULUS_TOKEN=from-file\nNEW_KEY_X=v\n")
    monkeypatch.delenv("NEW_KEY_X", raising=False)
    load_env_file(f)
    import os

    assert os.environ["METACULUS_TOKEN"] == "fake-metaculus" and os.environ["NEW_KEY_X"] == "v"
    monkeypatch.delenv("NEW_KEY_X")


def test_provider_billed_cost_goes_to_ledger(fake_llm):
    """OpenRouter usage.cost (вкл. плату за веб-поиск) важнее токенной цены."""
    fake_llm.billed_cost = 0.0421
    llm = guarded_llm.GuardedLlm("openrouter/google/gemini-3.5-flash:online", max_tokens=1000)
    asyncio.run(llm.invoke("hi"))
    (_, model, _, _, _, cost), = _rows()
    assert model == "openrouter/google/gemini-3.5-flash:online" and cost == pytest.approx(0.0421)


def test_online_suffix_uses_base_price_for_precheck(fake_llm):
    assert guarded_llm.price_key("openrouter/google/gemini-3.5-flash:online") == "openrouter/google/gemini-3.5-flash"
    llm = guarded_llm.GuardedLlm("openrouter/google/gemini-3.5-flash:online", max_tokens=1000)
    asyncio.run(llm.invoke("hi"))
    (_, _, _, _, _, cost), = _rows()
    assert cost == pytest.approx(1000 / 1000 * 0.0015 + 200 / 1000 * 0.009)
