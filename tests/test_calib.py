"""Калибровка (этап 4): все прогнозы до агрегации, их разброс и «сила справки» — в журнал, без новых вызовов ИИ."""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import types

import pytest
from fakes import FakeMetaculusClient, questions

from forecast_bot import calib, run as R, verify
from forecast_bot.journal import Journal


def test_summarize_binary_mc_numeric():
    s = calib.summarize([0.2, 0.3, 0.25, 0.4, 0.3])
    assert s["predictions_n"] == 5 and s["predictions_spread"] == pytest.approx(0.2)
    assert json.loads(s["predictions_all"]) == [0.2, 0.3, 0.25, 0.4, 0.3] and s["predictions_sd"] > 0
    opt = lambda n, p: types.SimpleNamespace(option_name=n, probability=p)  # noqa: E731
    mc = [types.SimpleNamespace(predicted_options=[opt("A", 0.5), opt("B", 0.5)]),
          types.SimpleNamespace(predicted_options=[opt("A", 0.8), opt("B", 0.2)])]
    assert calib.summarize(mc)["predictions_spread"] == pytest.approx(0.3)
    pct = lambda p, v: types.SimpleNamespace(percentile=p, value=v)  # noqa: E731
    num = [types.SimpleNamespace(declared_percentiles=[pct(0.4, 10), pct(0.6, 20)]),
           types.SimpleNamespace(declared_percentiles=[pct(0.4, 30), pct(0.6, 50)])]
    s = calib.summarize(num)
    assert json.loads(s["predictions_all"]) == [15.0, 40.0] and s["predictions_spread"] == pytest.approx(25.0)
    assert calib.summarize([])["predictions_n"] == 0


@pytest.mark.parametrize("meta,ok", [
    ("fred_series UNRATE", True), ("fetch_url https://www.bls.gov/news.release/cpi.htm", True),
    ("fetch_url https://ec.europa.eu/eurostat", True), ("fetch_url https://www.who.int/news", True),
    ("fetch_url https://www.reuters.com/x", False), ("fetch_url https://gov.evil.com/x", False),
    ("search_news inflation", False), ("stock_history AAPL", False),
])
def test_is_official(meta, ok):
    assert calib.is_official(meta) is ok


def test_verify_counts_cited_facts_and_sources():
    brief = ("Фон без чисел [S1]\nСтавка 4.25% [S2]\nВыдумка 77.7% [S1]\nСтрока без ссылки\nСм. вопрос [S0]")
    v = verify.check(brief, {"S0": "вопрос", "S1": "новость", "S2": "ставка составляет 4.25%"})
    assert v.facts_cited == 2 and v.cited_sources == {"S1", "S2"}     # выброшенная и [S0] не считаются


def _cycle_answers(fake_llm, values):
    it = itertools.cycle(values)
    orig = fake_llm._answer

    def answer(text):
        if '"Probability: ZZ%"' in text:
            return f"Рассуждение.\nProbability: {next(it)}%"
        if "You are a data analyst" in text and "BinaryPrediction" in text:  # парсер читает число из рассуждения
            m = re.findall(r"Probability: (\d+)%", text)
            if m:
                return f'{{"prediction_in_decimal": {int(m[-1]) / 100}}}'
        return orig(text)

    fake_llm._answer = answer


def test_all_predictions_and_spread_reach_journal_median_unchanged(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths
    from forecast_bot.bot import ForecastBot

    monkeypatch.delenv("FORECAST_PREDICTIONS", raising=False)            # бой: 5 прогнозов по умолчанию
    _cycle_answers(fake_llm, [20, 30, 25, 40, 30])
    client = FakeMetaculusClient(questions()[:1])
    result = asyncio.run(R.run(client=client, bot=ForecastBot(), journal=Journal(paths.journal_db()),
                               tournaments=["minibench"], submit=True))
    row = Journal(paths.journal_db()).rows(result.run_id)[0]
    assert row["predictions_n"] == 5 and sorted(json.loads(row["predictions_all"])) == [0.2, 0.25, 0.3, 0.3, 0.4]
    assert row["predictions_spread"] == pytest.approx(0.2)
    assert dict(client.predictions)[201]["probability_yes"] == pytest.approx(0.3)  # медиана — как раньше
    prompts = [p for p in fake_llm.prompts if '"Probability: ZZ%"' in p]
    assert len(prompts) == 5                                               # новых вызовов нет


def test_research_strength_reaches_journal(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import agent as A, paths
    from forecast_bot.bot import ForecastBot

    async def news(self, query):
        return "**Свежая новость** событие ещё не произошло"

    monkeypatch.setattr(ForecastBot, "_web_search", news)
    monkeypatch.setattr(A, "fred_series", lambda sid: f"FRED {sid}: последнее 4.1")
    monkeypatch.setenv("FORECAST_RESEARCH", "agent")
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    client = FakeMetaculusClient(questions()[:1])
    result = asyncio.run(R.run(client=client, bot=ForecastBot(), journal=Journal(paths.journal_db()),
                               tournaments=["t"], submit=False))
    row = Journal(paths.journal_db()).rows(result.run_id)[0]
    assert row["research_cited_facts"] == 2 and row["research_official"] == 1   # новость + FRED (официальный)
