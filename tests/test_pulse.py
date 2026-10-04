"""Market Pulse: сезоны по слагу, обновления при spot-очках, групповые подвопросы, подсказка quant."""
from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta

import pytest
from fakes import FakeMetaculusClient

from forecast_bot import quant as Q, run as R
from forecast_bot.journal import Journal


def group_post(pid=900, labels=("Oct 12 - Oct 23", "Oct 26 - Nov 6"), close_in_h=48):
    from datetime import datetime, timezone

    close = (datetime.now(timezone.utc) + timedelta(hours=close_in_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    subs = [{"id": pid * 10 + i, "title": f"NVDA vs MSFT ({lab})", "status": "open", "type": "numeric",
             "label": lab, "scheduled_close_time": close, "scheduled_resolve_time": "2026-12-31T00:00:00Z",
             "open_time": "2026-10-05T16:00:00Z", "my_forecasts": {"history": []}, "unit": "pp",
             "open_lower_bound": True, "open_upper_bound": True,
             "scaling": {"range_min": -15.0, "range_max": 15.0, "zero_point": None, "inbound_outcome_count": None}}
            for i, lab in enumerate(labels)]
    return {"id": pid, "title": "How much will Nvidia's stock price returns exceed Microsoft's in these biweekly periods of Q4 2026?",
            "projects": {"tournament": [{"slug": "market-pulse-26q4"}], "default_project": {"id": 33200}},
            "group_of_questions": {"questions": subs, "fine_print": "", "description": "", "resolution_criteria": ""}}


def subquestions(**kw):
    from forecasting_tools import MetaculusClient

    return MetaculusClient._unpack_group_question(group_post(**kw))


def test_pulse_slugs_around_quarter_boundaries():
    assert R.pulse_slugs(date(2026, 10, 4)) == ["market-pulse-26q3", "market-pulse-26q4", "market-pulse-27q1"]
    assert R.pulse_slugs(date(2027, 1, 2)) == ["market-pulse-26q4", "market-pulse-27q1", "market-pulse-27q2"]


def test_expand_tournaments_marks_pulse_as_spot():
    ids, refresh = R.expand_tournaments(["fall", "minibench", "pulse", "market-pulse-26q4"], date(2026, 10, 4))
    assert ids[:2] == [33121, "minibench"] and "market-pulse-26q4" in ids
    assert refresh == {"market-pulse-26q3", "market-pulse-26q4", "market-pulse-27q1"}
    ids, refresh = R.expand_tournaments(["fall", "33999"], date(2026, 10, 4))
    assert ids == [33121, 33999] and refresh == set()


@pytest.mark.parametrize("last_age_h,close_in_h,expect", [
    (None, 100, True), (2, 100, False), (25, 100, True),   # первый раз; свежий; суточное обновление
    (2, 10, False), (4, 10, True),                          # последние 12 ч — раз в 3 ч
])
def test_needs_update(last_age_h, close_in_h, expect):
    now = 1_000_000.0
    last = None if last_age_h is None else now - last_age_h * 3600
    assert R.needs_update(last, now + close_in_h * 3600, now) is expect


def _run(client, journal, tournaments, refresh, submit=True, **kw):
    from forecast_bot.bot import ForecastBot

    return asyncio.run(R.run(client=client, bot=ForecastBot(), journal=journal, tournaments=tournaments,
                             submit=submit, refresh=refresh, **kw))


def test_group_subquestions_forecast_and_refresh_on_schedule(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths

    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    journal = Journal(paths.journal_db())
    client = FakeMetaculusClient(subquestions())
    _run(client, journal, ["market-pulse-26q4"], {"market-pulse-26q4"})
    assert sorted(q for q, _ in client.predictions) == [9000, 9001]
    assert all("continuous_cdf" in p for _, p in client.predictions)
    assert {c["post_id"] for c in client.comments} == {900}

    _run(client, journal, ["market-pulse-26q4"], {"market-pulse-26q4"})   # сразу — не обновляем
    assert len(client.predictions) == 2

    conn = journal._conn()
    conn.execute("UPDATE forecasts SET submitted_at = submitted_at - 25 * 3600")
    conn.commit(); conn.close()
    _run(client, journal, ["market-pulse-26q4"], {"market-pulse-26q4"})   # прошли сутки — обновляем
    assert len(client.predictions) == 4


def test_non_spot_tournament_keeps_not_twice(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths

    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    journal = Journal(paths.journal_db())
    client = FakeMetaculusClient(subquestions())
    _run(client, journal, ["minibench"], set())
    conn = journal._conn()
    conn.execute("UPDATE forecasts SET submitted_at = submitted_at - 25 * 3600")
    conn.commit(); conn.close()
    _run(client, journal, ["minibench"], set())
    assert len(client.predictions) == 2


def test_missing_pulse_season_is_skipped_quietly(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths

    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")

    class Client(FakeMetaculusClient):
        def get_all_open_questions_from_tournament(self, t):
            if t == "market-pulse-27q1":
                raise RuntimeError("404 Not Found")
            return super().get_all_open_questions_from_tournament(t)

    client = Client(subquestions())
    res = _run(client, Journal(paths.journal_db()), ["market-pulse-27q1", "market-pulse-26q4"],
               {"market-pulse-27q1", "market-pulse-26q4"})
    assert res.count("ok") == 2


def _synthetic(key: str, n=900, drift=0.0005, seed=1):
    import numpy as np

    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    d0 = date(2023, 1, 2)
    dates = [d0 + timedelta(days=i) for i in range(n)]
    return Q.Series(key, dates, list(c), list(c * 1.01), list(c * 0.99), list(c), False)


def test_pulse_quant_percentiles_and_no_lookahead(monkeypatch):
    seen = {}

    def fake_load(key, cache_dir):
        s = _synthetic(key, seed=1 if "NVDA" in key else 2)
        seen[key] = s
        return s

    monkeypatch.setattr(Q, "load", fake_load)
    asof = date(2025, 1, 1)
    q = Q.pulse_quant("How much will Nvidia's stock price returns exceed Microsoft's in these biweekly periods",
                      "Jan 13 - Jan 24", 2025, asof, None)
    assert q.kind == "rel" and q.unit == "pp" and q.asof < asof
    vals = list(q.percentiles.values())
    assert vals == sorted(vals) and vals[0] < 0 < vals[-1]
    assert "STATISTICAL BASELINE" in q.hint() and "Assumed definition" in q.hint()
    # период уже начался — подсказки нет (исход частично известен, это не «база»)
    assert Q.pulse_quant("How much will Nvidia's stock price returns exceed Microsoft's", "Dec 1 - Dec 12",
                         2024, asof, None) is None
    assert Q.pulse_quant("Some unrelated group", "Jan 13 - Jan 24", 2025, asof, None) is None


def test_quant_hint_reaches_forecaster_only_with_flag(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths

    monkeypatch.setattr(Q, "load", lambda key, cache_dir: _synthetic(key, seed=3 if "NVDA" in key else 4))
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    monkeypatch.setenv("FORECAST_ASOF", "2025-06-01")
    qs = subquestions(labels=("Jun 9 - Jun 20",))
    for q in qs:
        q.close_time = q.close_time.replace(year=2025)
    _run(FakeMetaculusClient(qs), Journal(paths.journal_db()), ["x"], set(), submit=False)
    assert not any("STATISTICAL BASELINE" in p for p in fake_llm.prompts)
    monkeypatch.setenv("FORECAST_QUANT_HINTS", "1")
    _run(FakeMetaculusClient(qs), Journal(paths.journal_db()), ["x"], set(), submit=False)
    assert any("STATISTICAL BASELINE" in p and "Percentiles in pp" in p for p in fake_llm.prompts)


def test_market_series_question_skips_search(fake_llm, fake_asknews, monkeypatch):
    """С подсказкой quant поиск (агент/AskNews) не запускается — бережём квоту AskNews."""
    from forecast_bot import paths

    monkeypatch.setattr(Q, "load", lambda key, cache_dir: _synthetic(key, seed=5 if "NVDA" in key else 6))
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    monkeypatch.setenv("FORECAST_ASOF", "2025-06-01")
    monkeypatch.setenv("FORECAST_QUANT_HINTS", "1")
    monkeypatch.setenv("FORECAST_RESEARCH", "asknews-latest")
    calls = []

    async def fake_latest(self, query):
        calls.append(query)
        return "news"

    from forecast_bot.bot import ForecastBot

    monkeypatch.setattr(ForecastBot, "_asknews_latest", fake_latest)
    qs = subquestions(labels=("Jun 9 - Jun 20",))
    for q in qs:
        q.close_time = q.close_time.replace(year=2025)
    res = _run(FakeMetaculusClient(qs), Journal(paths.journal_db()), ["x"], set(), submit=False)
    assert res.count("ok") == 1 and calls == [] and res.rows[0]["asknews_calls"] == 0


class _R:
    def __init__(self, code):
        self.status_code = code


def test_tournament_exists_caches_missing_for_an_hour(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _R(404 if "27q1" in url else 200)

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(R, "_TOURNAMENT_SEEN", {})
    assert R.tournament_exists("market-pulse-27q1", now=1000.0) is False
    assert R.tournament_exists("market-pulse-27q1", now=1000.0 + 1800) is False and len(calls) == 1
    assert R.tournament_exists("market-pulse-27q1", now=1000.0 + 3700) is False and len(calls) == 2
    assert R.tournament_exists("market-pulse-26q4", now=1000.0) is True
    assert R.tournament_exists("market-pulse-26q4", now=1000.0 + 99999) is True and len(calls) == 3

    def boom(*a, **k):
        raise ConnectionError("offline")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(R, "_TOURNAMENT_SEEN", {})
    assert R.tournament_exists("market-pulse-26q4") is True and R._TOURNAMENT_SEEN == {}


def test_missing_season_is_not_fetched(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths

    fetched = []

    class Client(FakeMetaculusClient):
        def get_all_open_questions_from_tournament(self, t):
            fetched.append(t)
            return super().get_all_open_questions_from_tournament(t)

    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    monkeypatch.setattr(R, "tournament_exists", lambda slug, now=None: slug != "market-pulse-27q1")
    _run(Client(subquestions()), Journal(paths.journal_db()), ["market-pulse-27q1", "market-pulse-26q4"],
         {"market-pulse-27q1", "market-pulse-26q4"}, submit=False)
    assert fetched == ["market-pulse-26q4"]


def test_period_parsing():
    assert Q.period("Sep 21 - Oct 2", 2026) == (date(2026, 9, 21), date(2026, 10, 2))
    assert Q.period("Jul 13 - Jul 24", 2026) == (date(2026, 7, 13), date(2026, 7, 24))
    assert Q.period("Gross margin (GAAP)", 2026) is None
