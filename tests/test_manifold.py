"""Бэктест бота на Manifold: всё «как на дату t» — цена толпы, описание, справка, инструменты, деньги."""
from __future__ import annotations

import asyncio
import importlib
import json
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from forecast_bot import manifold as MF

UTC = timezone.utc
CLOSE = datetime(2026, 9, 20, tzinfo=UTC)


def _runner():
    return importlib.import_module("tools.manifold_backtest")


def _m(**kw):
    d = dict(id="m1", question="Will X happen by September 20, 2026?", description="Resolves YES if X.",
             created=datetime(2026, 8, 1, tzinfo=UTC), close=CLOSE, resolved_at=CLOSE + timedelta(hours=2),
             outcome=1, bettors=30, volume=5000.0, groups=["politics"], url="u")
    d.update(kw)
    return MF.MfMarket(**d)


def test_prob_at_strictly_before_t():
    t = CLOSE - timedelta(days=7)
    ts = t.timestamp() * 1000
    bets = [{"createdTime": ts - 7200, "probAfter": 0.3}, {"createdTime": ts - 60, "probAfter": 0.4},
            {"createdTime": ts, "probAfter": 0.9}, {"createdTime": ts + 60, "probAfter": 0.95},
            {"createdTime": ts - 30, "probAfter": 0.1, "isRedemption": True}]
    assert MF.prob_at(bets, t) == (0.4, 2)                                  # ставка в момент t и позже не видна
    assert MF.prob_at([{"createdTime": ts + 1, "probAfter": 0.5}], t) is None


def test_eligible_rules():
    lo, hi = datetime(2026, 7, 8, tzinfo=UTC), datetime(2026, 10, 7, tzinfo=UTC)
    assert MF.eligible(_m(), lo, hi) is None
    assert MF.eligible(_m(resolved_at=CLOSE - timedelta(days=8)), lo, hi) == "решён до t"
    assert MF.eligible(_m(bettors=5), lo, hi) == "мало торгов"
    assert MF.eligible(_m(created=CLOSE - timedelta(days=7, hours=12)), lo, hi) == "создан позже t − 1 сут"
    assert MF.eligible(_m(close=datetime(2026, 7, 1, tzinfo=UTC)), lo, hi) == "закрытие вне окна"
    assert MF.from_api({"id": "x", "outcomeType": "BINARY", "mechanism": "cpmm-1", "resolution": "CANCEL",
                        "createdTime": 1, "closeTime": 2}) is None


def test_dates_after_t_except_known():
    t = CLOSE - timedelta(days=7)
    brief = ("Talks set for September 18, 2026. Deal signed on 2026-09-15. Close September 20, 2026. "
             "Earlier, on July 3, 2026, nothing.")
    assert MF.dates_after(brief, t, CLOSE, known="summit on September 18, 2026") == [date(2026, 9, 15)]


def test_sample_is_hash_ordered_and_group_capped():
    ms = [_m(id=f"x{i}", groups=["a" if i < 20 else f"g{i}"]) for i in range(40)]
    s = MF.sample(ms, 15, per_group=3)
    assert len(s) == 15 and sum(m.group == "a" for m in s) <= 3
    assert [m.id for m in MF.sample(list(reversed(ms)), 15, per_group=3)] == [m.id for m in s]  # от порядка не зависит


def test_time_machine_cuts_series_and_disables_fetch(monkeypatch):
    R = _runner()
    from forecast_bot import agent as A
    from forecast_bot.bot import ForecastBot

    t = datetime(2026, 8, 10, tzinfo=UTC)
    monkeypatch.setattr(A, "fred_series", A.fred_series)
    monkeypatch.setattr(A, "stock_history", A.stock_history)
    monkeypatch.setattr(A, "fetch_url", A.fetch_url)
    monkeypatch.setattr(ForecastBot, "_web_search", ForecastBot._web_search)

    class Resp:
        text = "DATE,X\n2026-08-08,1.0\n2026-08-09,2.0\n2026-08-10,99.0\n2026-08-11,99.0\n"

    monkeypatch.setattr(R.requests, "get", lambda *a, **k: Resp())
    from forecast_bot.polymarket import gdelt

    arts = [gdelt.Article(t - timedelta(hours=3), "Fed signals pause", "u1", "d", ""),
            gdelt.Article(t - timedelta(days=2), "Weather news", "u2", "d", "")]
    R.install_time_machine(t, arts)
    out = A.fred_series("X")
    assert "99" not in out and "2026-08-09" in out                         # день t и позже отрезаны
    assert "недоступен в бэктесте" in A.fetch_url("https://example.com")
    res = asyncio.run(ForecastBot._web_search(None, "fed pause"))
    assert res.splitlines()[0].startswith("1. Fed signals pause")


def test_leak_check_unparsed_answer_is_leak(fake_llm, monkeypatch):
    R = _runner()
    from forecast_bot import guarded_llm

    monkeypatch.setattr(guarded_llm, "APP", "forecast-lab")
    guarded_llm.install_sentinel()
    monkeypatch.setattr(fake_llm, "_answer", staticmethod(lambda text: "не знаю"))
    assert asyncio.run(R.leak_check("bt:x", CLOSE, "q", "text"))[0] is True
    monkeypatch.setattr(fake_llm, "_answer", staticmethod(lambda text: '{"leak": false, "why": "ok"}'))
    assert asyncio.run(R.leak_check("bt:x", CLOSE, "q", "text")) == (False, "ok")


def test_forecast_skips_flagged_and_unchecked_and_stops_at_cap(tmp_path, monkeypatch, capsys):
    R = _runner()
    from forecast_bot import ai_guard, guarded_llm, paths

    monkeypatch.setattr(guarded_llm, "APP", "forecast-lab")
    mk = tmp_path / "manifold"
    monkeypatch.setattr(R, "d", lambda: mk)
    mk.mkdir()
    recs = [R.to_rec(_m(id=i), p_crowd=0.5, bets_before_t=5) for i in ("a", "b", "c")]
    (mk / "markets.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (mk / "desc_check.jsonl").write_text(json.dumps({"id": "a", "leak": True}) + "\n" +
                                         json.dumps({"id": "b", "leak": False}) + "\n")   # «c» не проверен
    monkeypatch.setattr(R, "STAGE_START", datetime(2026, 1, 1, tzinfo=UTC))
    conn = ai_guard._conn()
    conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                 (datetime(2026, 2, 1, tzinfo=UTC).timestamp(), "openrouter", "m", "forecast-lab:bt:x", 0, 0, 10.5))
    conn.commit(); conn.close()
    called = []
    monkeypatch.setattr(R, "install_time_machine", lambda *a: called.append(a))
    asyncio.run(R._forecast(types.SimpleNamespace(limit=0)))
    assert called == [] and "потолок этапа $10.0" in capsys.readouterr().out
    # без потолка: прогнозируется только «b» (a — утечка в описании, c — не проверен)
    conn = ai_guard._conn(); conn.execute("DELETE FROM usage"); conn.commit(); conn.close()
    seen = []

    async def fake_fq(self, q, return_exceptions=True):
        seen.append(q.question_text)
        return types.SimpleNamespace(prediction=0.6)

    from forecast_bot.bot import ForecastBot

    monkeypatch.setattr(ForecastBot, "forecast_question", fake_fq)
    monkeypatch.setattr(R, "leak_check", lambda *a: _done((False, "")))
    asyncio.run(R._forecast(types.SimpleNamespace(limit=0)))
    rows = [json.loads(l) for l in (mk / "results.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["b"] and rows[0]["p_bot"] == 0.6 and len(called) == 1


async def _coro(v):
    return v


def _done(v):
    return _coro(v)
