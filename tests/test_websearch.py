"""Веб-поиск OpenRouter + Exa вместо AskNews: учёт в леджере отдельной строкой, источники с URL, [S#]."""
from __future__ import annotations

import asyncio

import pytest
from fakes import FakeMetaculusClient, questions

from forecast_bot import agent as A, ai_guard, guarded_llm, run as R, websearch
from forecast_bot.journal import Journal


def _ledger():
    conn = ai_guard._conn()
    try:
        return conn.execute('SELECT model, "user", cost_usd FROM usage ORDER BY rowid').fetchall()
    finally:
        conn.close()


def test_split_search_cost_real_numbers():
    """Живой ответ 05.10.2026: cost 0.010201 = upstream 0.003201 + Exa 0.007."""
    model, search, n = guarded_llm.split_search_cost(
        {"cost": 0.010201, "cost_details": {"upstream_inference_cost": 0.003201}}, None, 0.007)
    assert (round(model, 6), round(search, 6), n) == (0.003201, 0.007, 1)


def test_split_search_cost_prefers_fact_over_price():
    """Факт расходится с прайсом (например, Exa подорожал) — в леджер идёт факт, а не $0.007."""
    model, search, _ = guarded_llm.split_search_cost(
        {"cost": 0.0175, "cost_details": {"upstream_inference_cost": 0.0035}}, None, 0.007)
    assert model == pytest.approx(0.0035) and search == pytest.approx(0.014)


def test_split_search_cost_fallback_to_price():
    model, search, n = guarded_llm.split_search_cost({"server_tool_use": {"web_search_requests": 2}}, 0.02, 0.007)
    assert n == 2 and search == pytest.approx(0.014) and model == pytest.approx(0.006)
    model, search, _ = guarded_llm.split_search_cost({}, None, 0.007)
    assert model is None and search == pytest.approx(0.007)


def test_search_writes_two_ledger_rows_and_returns_sources(fake_llm):
    token = guarded_llm.CURRENT_USER.set("q5")
    try:
        text = asyncio.run(websearch.search("ставка ФРС октябрь 2026"))
    finally:
        guarded_llm.CURRENT_USER.reset(token)
    assert "https://example.org/news/1" in text and "3.75%" in text
    rows = _ledger()
    assert [(m, u) for m, u, _ in rows] == [(websearch.search_model(), "forecast:q5"),
                                            (guarded_llm.SEARCH_ROW_MODEL, "forecast:q5")]
    assert rows[0][2] == pytest.approx(0.003201) and rows[1][2] == pytest.approx(0.007)
    assert guarded_llm.GUARDED_CALLS["q5"] == 2  # строк леджера = учтённых вызовов (сверка в run.py)


def test_agent_with_web_search_end_to_end(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths
    from forecast_bot.bot import ForecastBot

    monkeypatch.setattr(A, "fred_series", lambda sid: f"FRED {sid}: последнее 4.1")
    monkeypatch.setenv("FORECAST_RESEARCH", "agent")
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    fake_llm.tool_plan = ["search_news"]
    res = asyncio.run(R.run(client=FakeMetaculusClient(questions()[:1]), bot=ForecastBot(),
                            journal=Journal(paths.journal_db()), tournaments=["t"], submit=False))
    row, = res.rows
    assert row["status"] == "ok" and row["web_searches"] == 1 and row["asknews_calls"] == 0
    assert fake_llm.web_searches == 1 and fake_asknews == []
    forecast_prompt = next(p for p in fake_llm.prompts if '"Probability: ZZ%"' in p)
    assert "Заголовок 1" in forecast_prompt  # источник из веб-поиска дошёл до прогнозиста (через [S#])


def test_asknews_forbidden_outside_battle(monkeypatch):
    """Урок 05.10.2026: замеры съели боевой кошелёк AskNews — вне приложения forecast вызов запрещён до сети."""
    from forecast_bot.bot import ForecastBot

    monkeypatch.setattr(guarded_llm, "APP", "forecast-lab")
    with pytest.raises(RuntimeError, match="AskNews запрещён вне боя"):
        asyncio.run(ForecastBot()._asknews_latest("anything"))


def test_web_is_default_search_and_asknews_needs_flag(monkeypatch):
    from forecast_bot.bot import ForecastBot

    monkeypatch.delenv("FORECAST_SEARCH", raising=False)
    monkeypatch.setenv("FORECAST_RESEARCH", "agent")
    bot = ForecastBot()
    assert bot.search_backend() == "web" and bot.asknews_calls_per_research == 0
    monkeypatch.setenv("FORECAST_SEARCH", "asknews")
    assert bot.asknews_calls_per_research == bot.agent_max_searches
