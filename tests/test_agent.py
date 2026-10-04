"""Агентный исследователь: цикл с инструментами, лимиты шагов/новостей/денег, учёт в леджере, SSRF-защита."""
from __future__ import annotations

import asyncio
import json

import pytest
from fakes import FakeMetaculusClient, questions

from forecast_bot import agent as A, ai_guard, guarded_llm, run as R
from forecast_bot.journal import Journal


@pytest.fixture
def agent_env(monkeypatch, fake_llm, fake_asknews):
    from forecast_bot.bot import ForecastBot

    news_queries: list[str] = []

    async def fake_news(self, query):
        news_queries.append(query)
        return "**Свежая новость** событие ещё не произошло"

    monkeypatch.setattr(ForecastBot, "_asknews_latest", fake_news)
    monkeypatch.setattr(A, "fred_series", lambda sid: f"FRED {sid}: последнее 4.1")
    monkeypatch.setattr(A, "stock_history", lambda s: f"Stooq {s}: 100")
    monkeypatch.setattr(A, "fetch_url", lambda u: "страница")
    monkeypatch.setenv("FORECAST_RESEARCH", "agent")
    monkeypatch.setenv("FORECAST_MODEL", "openrouter/anthropic/claude-opus-5.5")
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    return news_queries


def _run_one(qs=None):
    from forecast_bot import paths
    from forecast_bot.bot import ForecastBot

    client = FakeMetaculusClient(qs or questions()[:1])
    result = asyncio.run(R.run(client=client, bot=ForecastBot(), journal=Journal(paths.journal_db()),
                               tournaments=["t"], submit=False))
    return result, client


def test_agent_research_reaches_forecaster_and_ledger(agent_env, fake_llm):
    result, _ = _run_one()
    row, = result.rows
    assert row["status"] == "ok" and row["asknews_calls"] == 1 and agent_env == ["событие"]
    forecast_prompts = [p for p in fake_llm.prompts if '"Probability: ZZ%"' in p]
    assert forecast_prompts and "Справка агента" in forecast_prompts[0] and "FRED UNRATE" in forecast_prompts[0]
    conn = ai_guard._conn()
    n = conn.execute('SELECT COUNT(*) FROM usage WHERE "user" = ?', ("forecast:q201",)).fetchone()[0]
    conn.close()
    assert n == len(fake_llm.calls) == row["llm_calls"]


def test_news_calls_capped_per_question(agent_env, fake_llm):
    fake_llm.tool_plan = ["search_news"] * 5
    result, _ = _run_one()
    assert result.rows[0]["asknews_calls"] == A.MAX_NEWS_CALLS == len(agent_env)


def test_steps_capped_and_last_step_has_no_tool_use(agent_env, fake_llm):
    fake_llm.tool_plan = ["fred_series"] * 20
    _run_one()
    agent_calls = [k for k in fake_llm.kwargs if k.get("tools")]
    assert len(agent_calls) == A.MAX_STEPS + 1
    assert agent_calls[-1]["tool_choice"] == "none" and all(k["tool_choice"] == "auto" for k in agent_calls[:-1])


def test_soft_budget_stop_forces_final_brief(agent_env, fake_llm, monkeypatch):
    monkeypatch.setenv("FORECAST_QUESTION_BUDGET", "0.001")  # первый же вызов Opus ($0.008) превышает 60%
    fake_llm.tool_plan = ["fred_series"] * 5
    result, _ = _run_one()
    agent_calls = [k for k in fake_llm.kwargs if k.get("tools")]
    assert [k["tool_choice"] for k in agent_calls] == ["auto", "none"]
    assert result.rows[0]["status"] == "ok"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://localhost/", "file:///etc/passwd",
                                 "http://10.0.0.5/admin", "ftp://example.com/x"])
def test_fetch_url_refuses_non_public(url):
    assert A.fetch_url(url).startswith("Отказ")


def test_reasoning_flag_sets_effort_and_drops_temperature(monkeypatch):
    from forecast_bot.bot import build_llms

    monkeypatch.setenv("FORECAST_REASONING", "high")
    llm = build_llms()["default"]
    assert llm.litellm_kwargs.get("reasoning_effort") == "high" and llm.litellm_kwargs.get("temperature") is None
    monkeypatch.delenv("FORECAST_REASONING")
    assert build_llms()["default"].litellm_kwargs.get("temperature") == 0.3


def test_agent_mode_requires_asknews_key():
    base = {"METACULUS_TOKEN": "t", "OPENROUTER_API_KEY": "k", "FORECAST_RESEARCH": "agent"}
    assert R.missing_keys(base) == ["ASKNEWS_API_KEY"]


def test_guarded_completion_counts_and_blocks_budget(fake_llm, monkeypatch):
    guarded_llm.start_run(0.0)  # лимит запуска 0 → отказ до вызова
    with pytest.raises(ai_guard.BudgetExceeded):
        asyncio.run(guarded_llm.guarded_completion("openrouter/anthropic/claude-opus-5.5",
                                                   [{"role": "user", "content": "x"}], max_tokens=100))
    assert fake_llm.calls == []


class _Resp:
    def __init__(self, payload=None, text="<html>verify you are human</html>"):
        self._payload, self.text = payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_stock_history_reads_yahoo(monkeypatch):
    payload = {"chart": {"result": [{"meta": {"gmtoffset": -14400},
                                     "timestamp": [1790000000 + 86400 * i for i in range(70)],
                                     "indicators": {"quote": [{"close": [100 + i for i in range(69)] + [None]}]}}]}}
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        return _Resp(payload)

    monkeypatch.setattr(A.requests, "get", fake_get)
    out = A.stock_history("aapl")
    assert "query1.finance.yahoo.com" in seen["url"] and seen["url"].endswith("/AAPL")
    assert out.startswith("Yahoo AAPL: 69 наблюдений") and "последнее 168" in out


def test_stock_history_challenge_page_is_explicit_error(monkeypatch):
    monkeypatch.setattr(A.requests, "get", lambda *a, **k: _Resp(None))
    assert A.stock_history("^GSPC").startswith("Ошибка Yahoo для ^GSPC")


def test_no_tool_points_to_stooq():
    assert not any("stooq" in json.dumps(t).lower() for t in A.TOOLS)
