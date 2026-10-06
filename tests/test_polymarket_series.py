"""Бэктест №2 «ряды данных»: разбор рынков, данные строго до t, модель, поправка LLM через гард, отчёт."""
from __future__ import annotations

import asyncio
import importlib
import json
import types
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from forecast_bot.polymarket import backtest as B, http, series as S
from forecast_bot.polymarket import series_backtest as SB, series_data as SD, series_llm as L, series_quant as Q

UTC = timezone.utc
H = 3600


def _runner():
    return importlib.import_module("tools.polymarket_series")  # через sys.modules — чтобы мутации его видели


# ─────────────────────────── разбор ─────────────────────────────────
@pytest.mark.parametrize("title,want", [
    ("Bitcoin above ___ on September 24?", ("crypto_close", "binance:BTCUSDT")),
    ("Ethereum price on September 3?", ("crypto_bracket", "binance:ETHUSDT")),
    ("What price will Bitcoin hit September 21-27?", ("crypto_hit", "binance:BTCUSDT")),
    ("What will US Dollar Index (DXY) hit Week of September 21 2026?", ("dxy_hit", "yahoo:DX-Y.NYB")),
    ("What will WTI Crude Oil (WTI) hit in August 2026?", ("wti_hit", "yahoo:CL=F")),
    ("How low will 10-year Treasury yield get in September?", ("ust_hit", "treasury:10 Yr")),
    ("August Inflation US - Monthly", ("cpi", "macro:cpi_mom")),
    ("Core CPI YoY - August 2026", ("core_cpi", "macro:core_yoy")),
    ("JOLTS Job Openings — July 2026", ("jolts", "macro:jolts")),
    ("Bitcoin Up or Down on September 24?", None),
    ("Bitcoin ETF Flows on September 1?", None),
    ("Will USD be at least 3.1M Iranian rials on September 30?", None),
    ("What will the Bitcoin Implied Volatility index hit by August?", None),
    ("Bitcoin above ___ on September 24, 4PM ET?", None),
    ("How high will 7-year Treasury yield go in September?", None),
])
def test_classify_whitelist(title, want):
    assert S.classify(title) == want


def test_parse_strikes():
    p = S.parse_strike
    assert p("crypto_close", "2,500", "Will the price of Ethereum be above $2,500 on September 24?", "") == \
        S.Spec("close_above", lo=2500)
    assert p("crypto_bracket", "<68,000", "", "") == S.Spec("bracket", hi=68000)
    assert p("crypto_bracket", ">86,000", "", "") == S.Spec("bracket", lo=86000)
    assert p("crypto_bracket", "70,000-72,000", "", "") == S.Spec("bracket", lo=70000, hi=72000)
    assert p("crypto_hit", "↓ 80,000", "", "") == S.Spec("hit_low", lo=80000)
    assert p("wti_hit", "↑ $115", "", "") == S.Spec("hit_high", lo=115)
    assert p("ust_hit", "4.82%", "", "") == S.Spec("close_hit_high", lo=4.82)
    assert p("ust_hit", "Below 4.45%", "", "") == S.Spec("close_hit_low", lo=4.45)
    end = datetime(2026, 9, 11, 3, 59, tzinfo=UTC)
    assert p("cpi", "-0.2%", "", "August Inflation US - Monthly", end) == \
        S.Spec("bucket", lo=-0.25, hi=-0.15, target_month="2026-08")
    assert p("core_cpi", "0.6%+", "", "Core CPI MoM - August 2026") == \
        S.Spec("bucket", lo=0.55, target_month="2026-08")
    assert p("cpi", "≤2.9%", "", "August Inflation US - Annual", end) == \
        S.Spec("bucket", hi=2.95, target_month="2026-08")
    assert p("jolts", "7.0M to 7.1M", "", "JOLTS Job Openings: August 2026") == \
        S.Spec("bucket", lo=7000, hi=7100, target_month="2026-08")
    assert p("jolts", "<7.0M", "", "JOLTS Job Openings: August 2026").hi == 7000
    assert p("crypto_close", "abc", "", "") is None and p("crypto_hit", "96,000", "", "") is None
    # декабрьские данные выходят в январе: год — предыдущий
    assert p("cpi", "0.3%", "", "December Inflation US - Monthly", datetime(2027, 1, 14, tzinfo=UTC)).target_month == \
        "2026-12"


def _raw(i, item, start="2026-09-17T16:00:00Z", end="2026-09-24T16:00:00Z", closed="2026-09-24 16:12:00+00",
         prices='["0", "1"]', fee="crypto_fees_v2"):
    return {"id": str(100 + i), "question": f"Will the price of Bitcoin be above ${item} on September 24?",
            "slug": f"s{i}", "description": "Binance BTC/USDT 12:00 ET", "startDate": start, "endDate": end,
            "closedTime": closed, "volumeNum": "5000", "outcomes": '["Yes", "No"]', "outcomePrices": prices,
            "clobTokenIds": f'["{i}1", "{i}2"]', "umaResolutionStatus": "resolved", "feeType": fee,
            "groupItemTitle": item}


def test_from_event_dedupes_thins_and_filters_life():
    ev = {"id": 9, "title": "Bitcoin above ___ on September 24?",
          "markets": [_raw(i, f"{60 + 2 * i},000") for i in range(11)] + [_raw(99, "60,000")]}
    ms = S.from_event(ev)
    assert len(ms) == S.MAX_STRIKES and len({m.spec.lo for m in ms}) == S.MAX_STRIKES
    assert ms[0].spec.lo == 60000 and ms[-1].spec.lo == 80000         # края сетки сохранены
    assert all(m.fee_rate == 0.07 and m.cls == "crypto_close" for m in ms)
    short = {"id": 10, "title": "Bitcoin above ___ on September 24?",
             "markets": [_raw(1, "60,000", start="2026-09-23T12:00:00Z")]}
    assert S.from_event(short) == []                                   # живёт ~1 сут — t48 нет


def test_points_from_planned_end_not_close_time():
    """«Hit» закрылся 09.09 в момент касания, плановый конец — 30.09: точки — от 30.09, закрытые до t — вон."""
    ev = {"id": 5, "title": "How high will 10-year Treasury yield go in September?", "markets": [
        _raw(1, "4.82%", start="2026-09-03T01:05:31Z", end="2026-09-30T12:00:00Z", closed="2026-09-09 22:05:25+00",
             prices='["1", "0"]', fee="economics_fees"),
        _raw(2, "5.00%", start="2026-09-03T01:05:31Z", end="2026-09-30T12:00:00Z", closed="2026-09-30 23:00:00+00",
             fee="economics_fees")]}
    early, late = S.from_event(ev)
    assert early.spec.lo == 4.82 and late.spec.lo == 5.00
    assert S.points(late)["t48"] == datetime(2026, 9, 28, 12, tzinfo=UTC)
    assert S.points(early) == {}                                       # обе точки после закрытия 09.09
    assert late.resolve_at() == datetime(2026, 9, 30, 23, tzinfo=UTC)


# ─────────────────────────── данные строго до t ─────────────────────
def _bars(n=24 * 400, start=datetime(2025, 8, 1, tzinfo=UTC), seed=1, diff=False):
    rng = np.random.default_rng(seed)
    a = np.array([start.timestamp() + (i + 1) * H for i in range(n)])
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n))) if not diff else 4 + np.cumsum(rng.normal(0, 0.01, n))
    return SD.Bars("x", a, c, c * (1.001 if not diff else 1), c * (0.999 if not diff else 1), diff)


def test_bars_upto_is_strict():
    b = _bars(100)
    t = b.avail[49]
    assert len(b.upto(t)) == 50 and b.upto(t).avail[-1] == t
    assert len(b.upto(t - 1)) == 49


def test_price_dist_ignores_future_bars():
    """Шип в будущем после t не меняет распределение на t — модель видит только известное."""
    b = _bars()
    t = b.avail[-1] - 30 * 86400
    base = Q.price_dist(b, t, t + 48 * H)
    spoiled = SD.Bars("x", b.avail, b.close.copy(), b.high.copy(), b.low.copy())
    k = int(np.searchsorted(b.avail, t, side="right"))
    spoiled.close[k:] *= 50
    spoiled.high[k:] *= 50
    after = Q.price_dist(spoiled, t, t + 48 * H)
    assert base.s0 == after.s0 and np.allclose(base.final, after.final) and np.allclose(base.mx, after.mx)
    assert base.info["analogs"] >= Q.MIN_ANALOGS and base.horizon_s == pytest.approx(48 * H, abs=H)


def test_price_dist_rejects_stale_series():
    b = _bars(24 * 200)
    assert Q.price_dist(b, b.avail[-1] + 10 * 86400, b.avail[-1] + 12 * 86400) is None


def test_prob_shapes():
    b = _bars()
    t = b.avail[-1] - 10 * 86400
    d = Q.price_dist(b, t, t + 72 * H)
    s0 = d.s0
    ks = [s0 * f for f in (0.9, 0.97, 1.0, 1.03, 1.1)]
    above = [Q.prob(S.Spec("close_above", lo=k), d) for k in ks]
    hit = [Q.prob(S.Spec("hit_high", lo=k), d) for k in ks]
    assert above == sorted(above, reverse=True) and hit == sorted(hit, reverse=True)
    assert all(h >= a - 1e-9 for h, a in zip(hit[2:], above[2:]))     # коснуться не труднее, чем закрыться выше
    edges = [-S.INF, *ks, S.INF]
    raw = [Q._cdf(d.final, d.x(hi) if hi != S.INF else S.INF) - Q._cdf(d.final, d.x(lo) if lo != -S.INF else -S.INF)
           for lo, hi in zip(edges, edges[1:])]
    assert sum(raw) == pytest.approx(1.0, abs=1e-6)
    up = Q.prob(S.Spec("close_above", lo=s0 * 1.03), d, shift_sigma=1.0)
    wide = Q.prob(S.Spec("close_above", lo=s0 * 1.1), d, vol_mult=2.0)
    assert up > above[3] and wide > above[4]


def test_already_hit_drops_point():
    seen = SD.Bars("x", np.array([1.0, 2.0]), np.array([100.0, 101.0]), np.array([100.5, 103.0]),
                   np.array([99.0, 100.0]))
    assert Q.already_hit(S.Spec("hit_high", lo=102), seen)
    assert not Q.already_hit(S.Spec("hit_high", lo=104), seen)
    assert Q.already_hit(S.Spec("hit_low", lo=99), seen) and not Q.already_hit(S.Spec("close_hit_low", lo=99), seen)
    assert not Q.already_hit(S.Spec("close_above", lo=50), seen)


class _Store:
    """Фейковый ALFRED: значение месяца публикуется в `pub[месяц]`; винтаж отдаёт опубликованное к дате."""

    def __init__(self, series: dict[str, dict[date, tuple[float, date]]]):
        self.series, self.asked = series, []

    def vintage(self, sid, vint):
        self.asked.append((sid, vint))
        return sorted((d, v) for d, (v, pub) in self.series[sid].items() if pub <= vint)


def _monthly(n=60, start=date(2021, 9, 1), v0=7000.0, step=10.0, lag_days=35):
    out, d, v = {}, start, v0
    for i in range(n):
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        out[d] = (v, nxt + timedelta(days=lag_days))
        d, v = nxt, v + step * (1 if i % 3 else -1)
    return out


def test_macro_uses_vintage_before_t_and_refuses_published_month():
    st = _Store({"JTSJOL": _monthly()})
    target = date(2026, 7, 1)
    pub = st.series["JTSJOL"][target][1]
    t_before = datetime(pub.year, pub.month, pub.day, tzinfo=UTC) - timedelta(days=2)
    d, why = Q.macro_dist("macro:jolts", "2026-07", t_before, st)
    assert why == "" and d is not None and st.asked[-1][1] < t_before.date()
    d2, why2 = Q.macro_dist("macro:jolts", "2026-07", t_before + timedelta(days=5), st)
    assert d2 is None and why2 == "значение уже опубликовано на t"
    p = Q.macro_prob(S.Spec("bucket", lo=-S.INF, hi=S.INF, target_month="2026-07"), d)
    assert p == pytest.approx(1 - Q.FLOOR)


def test_macro_yoy_and_mom():
    idx = {}
    d0 = date(2020, 1, 1)
    for i in range(80):
        d = date(d0.year + (d0.month - 1 + i) // 12, (d0.month - 1 + i) % 12 + 1, 1)
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 15)
        idx[d] = (100 * 1.0025 ** i, nxt)
    st = _Store({"CPIAUCNS": idx, "CPIAUCSL": idx, "CPILFESL": idx})
    t = datetime(2026, 9, 9, tzinfo=UTC)
    yoy, why = Q.macro_dist("macro:cpi_yoy", "2026-08", t, st)
    assert why == "" and yoy.center == pytest.approx(100 * (1.0025 ** 12 - 1), abs=0.01)
    mom, _ = Q.macro_dist("macro:core_mom", "2026-08", t, st)
    assert mom.center == pytest.approx(0.25, abs=1e-6)


# ─────────────────────────── LLM ───────────────────────────────────
def test_parse_answer_clips_and_rejects():
    assert L.parse_answer('Думаю так: {"shift_sigma": 3, "vol_mult": 0.1, "reason": "ok"}') == (1.5, 0.5, "ok")
    assert L.parse_answer('{"shift_sigma": -0.4, "vol_mult": 1.2}')[:2] == (-0.4, 1.2)
    for bad in ("нет json", '{"shift_sigma": "x", "vol_mult": 1}', '{"vol_mult": 1}', '{"shift_sigma": NaN, "vol_mult": 1}'):
        with pytest.raises(L.BadAnswer):
            L.parse_answer(bad)


def _write_fixture(tmp_path, monkeypatch, p_mkt=0.3141):
    """Каталог данных с одним событием крипты: рынок, кэш часового ряда, строки quant, заголовки GDELT."""
    R = _runner()
    monkeypatch.setattr(SB, "data_dir", lambda: tmp_path)
    b = _bars(start=datetime(2025, 8, 1, tzinfo=UTC))
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "binance_BTCUSDT.json").write_text(json.dumps(
        {"key": "binance:BTCUSDT", "avail": b.avail.tolist(), "close": b.close.tolist(), "high": b.high.tolist(),
         "low": b.low.tolist(), "diff": False}))
    end = datetime.fromtimestamp(b.avail[-1], UTC).replace(minute=0, second=0) - timedelta(days=5)
    start = end - timedelta(days=7)
    ev = {"id": 1, "title": "Bitcoin above ___ on September 24?",
          "markets": [_raw(i, f"{int(b.close[-1] * f):,}", start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           end=end.strftime("%Y-%m-%dT%H:%M:%SZ"), closed=end.strftime("%Y-%m-%d %H:%M:%S+00"))
                      for i, f in enumerate((0.95, 1.0, 1.05))]}
    ms = S.from_event(ev)
    for m in ms:
        m.history = [(int((start + timedelta(hours=h)).timestamp()), p_mkt) for h in range(0, 24 * 7, 2)]
    (tmp_path / "markets.jsonl").write_text("".join(m.to_json() + "\n" for m in ms))
    monkeypatch.setattr(B, "CUTOFF", start - timedelta(days=1))
    rows, skip = R.quant_rows(R.load_markets(), R.store())
    (tmp_path / "quant.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    news = []
    for r in rows:
        t = datetime.fromisoformat(r["t"])
        news.append({"key": f"{r['group']}|{r['point']}", "t": r["t"], "articles": [
            {"seen": (t - timedelta(hours=5)).isoformat(), "title": "ETF inflows surge", "url": "u", "domain": "d"},
            {"seen": (t + timedelta(hours=5)).isoformat(), "title": "BITCOIN CRASHES TOMORROW", "url": "u",
             "domain": "d"},
            {"seen": (t - timedelta(minutes=20)).isoformat(), "title": "FED SURPRISE AFTER MARKET PRICE", "url": "u",
             "domain": "d"}]})
    (tmp_path / "gdelt.jsonl").write_text("".join(json.dumps(x) + "\n" for x in {n["key"]: n for n in news}.values()))
    return R, ms, rows


def test_quant_rows_fixture(tmp_path, monkeypatch):
    R, ms, rows = _write_fixture(tmp_path, monkeypatch)
    assert len(ms) == 3 and {r["point"] for r in rows} == {"t50", "t48"} and len(rows) == 6
    assert all(r["group"] == rows[0]["group"] and 0 < r["p_quant"] < 1 for r in rows)
    ps = [r["p_quant"] for r in rows if r["point"] == "t48"]
    assert ps == sorted(ps, reverse=True)


def test_llm_through_guard_without_market_price_and_future_news(tmp_path, monkeypatch, fake_llm):
    from forecast_bot import ai_guard, guarded_llm

    R, ms, rows = _write_fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(ai_guard.APP_LIMITS, "polymarket", dict(ai_guard.APP_LIMITS["polymarket"]))
    monkeypatch.setattr(guarded_llm, "APP", "polymarket")
    monkeypatch.setattr(fake_llm, "_answer", staticmethod(lambda text: '{"shift_sigma": 0.5, "vol_mult": 1.0, '
                                                                       '"reason": "inflows"}'))
    asyncio.run(R.run_llm(types.SimpleNamespace(limit=0, first_point="t48")))
    recs = [json.loads(l) for l in (tmp_path / "llm.jsonl").read_text().splitlines()]
    assert len(recs) == 2 and all("error" not in r for r in recs)      # одна группа × 2 точки = 2 вызова
    assert len(fake_llm.prompts) == 2
    for prompt in fake_llm.prompts:
        assert "0.3141" not in prompt and "31.4" not in prompt          # цены рынка LLM не видит
        assert "ETF inflows surge" in prompt and "CRASHES TOMORROW" not in prompt  # новости строго до t
        assert "AFTER MARKET PRICE" not in prompt     # и не позже цены рынка, с которой идёт сравнение
    # каждый вызов — строка леджера приложения polymarket под pm2
    assert SB.stage_spent() > 0 and all(r["llm_calls"] == 1 for r in recs)
    for r in recs:
        q = {x["market"]: x["p_quant"] for x in rows if x["point"] == r["point"]}
        assert all(r["p_llm"][mid] >= q[mid] - 1e-9 for mid in q)        # сдвиг вверх поднимает «above»
    joined = R.joined_rows()
    assert all(x["p_llm"] is not None for x in joined)
    assert guarded_llm.GUARDED_CALLS[f"{SB.LEDGER_PREFIX}:{recs[0]['group']}-{recs[0]['point']}"] == 1


def test_llm_bad_answer_is_error_not_zero_shift(tmp_path, monkeypatch, fake_llm):
    from forecast_bot import ai_guard, guarded_llm

    R, ms, rows = _write_fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(ai_guard.APP_LIMITS, "polymarket", dict(ai_guard.APP_LIMITS["polymarket"]))
    monkeypatch.setattr(guarded_llm, "APP", "polymarket")
    monkeypatch.setattr(fake_llm, "_answer", staticmethod(lambda text: "не знаю"))
    asyncio.run(R.run_llm(types.SimpleNamespace(limit=0, first_point="t48")))
    recs = [json.loads(l) for l in (tmp_path / "llm.jsonl").read_text().splitlines()]
    assert recs and all("error" in r and "p_llm" not in r for r in recs)


def test_llm_stops_at_stage_cap(tmp_path, monkeypatch, fake_llm, capsys):
    from forecast_bot import ai_guard, guarded_llm

    R, ms, rows = _write_fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(ai_guard.APP_LIMITS, "polymarket", dict(ai_guard.APP_LIMITS["polymarket"]))
    monkeypatch.setattr(guarded_llm, "APP", "polymarket")
    conn = ai_guard._conn()
    conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                 (SB.STAGE2_START.timestamp() + 60, "openrouter", "m", "polymarket:pm2:x", 0, 0, 20.01))
    conn.commit(); conn.close()
    asyncio.run(R.run_llm(types.SimpleNamespace(limit=0, first_point="t48")))
    assert fake_llm.prompts == [] and not (tmp_path / "llm.jsonl").exists()
    # стоп именно по потолку этапа (явно), а не по лимиту запуска гарда — тот второй рубеж
    assert "потолок этапа $20.0 исчерпан" in capsys.readouterr().out


def test_stage2_budget_counts_only_pm2():
    from forecast_bot import ai_guard

    assert SB.stage_budget_left() == pytest.approx(SB.STAGE2_CAP_USD)
    conn = ai_guard._conn()
    for user, cost, dt in (("polymarket:pm123-t48-gdelt", 13.5, 60), ("polymarket:pm2:ev1-t48", 1.25, 60),
                           ("polymarket:pm2:ev2-t48", 7.0, -60)):
        conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                     (SB.STAGE2_START.timestamp() + dt, "openrouter", "m", user, 0, 0, cost))
    conn.commit(); conn.close()
    assert SB.stage_spent() == pytest.approx(1.25)


# ─────────────────────────── отчёт ─────────────────────────────────
def _row(i, cluster, p_quant, p_mkt, y, volume=5000.0, fee=0.07, family="крипта", p_llm=None):
    return {"market": f"m{i}", "cluster": cluster, "family": family, "point": "t48", "p_quant": p_quant,
            "p_llm": p_llm, "p_mkt": p_mkt, "outcome": y, "fee_rate": fee, "volume": volume}


def test_trades_skip_micro_and_pay_fee():
    rows = [_row(1, "a", 0.8, 0.5, 1), _row(2, "a", 0.8, 0.5, 1, volume=500)]
    ts = SB.trades(rows, "p_quant", 0.10)
    assert len(ts) == 1 and ts[0].fee == pytest.approx(0.07 * 0.515 * 0.485)


def test_cluster_bootstrap():
    rows = [_row(i, f"c{i // 5}", 0.6, 0.5, i % 2) for i in range(40)]
    ci = SB.bootstrap(rows, lambda s: sum(r["outcome"] for r in s) / len(s), n=300)
    assert ci[0] <= 0.5 <= ci[1]
    assert SB.bootstrap([_row(1, "only", 0.6, 0.5, 1)], lambda s: 1.0) is None


def test_bootstrap_resamples_clusters_not_rows():
    """Один кластер из 30 одинаковых строк против 30 кластеров по одной — разброс разный."""
    one = [_row(i, "big", 0.9, 0.5, 1) for i in range(30)] + [_row(100, "small", 0.9, 0.5, 0)]
    ci = SB.bootstrap(one, lambda s: sum(r["outcome"] for r in s) / len(s), n=400)
    assert ci[0] < 0.5   # примерно в четверти выборок «big» не попадает — доля падает до нуля


def test_report_tables():
    rows = [_row(i, f"c{i % 6}", 0.7 if i % 2 else 0.3, 0.5, i % 2, p_llm=0.6) for i in range(24)]
    out = SB.report(rows, boot=100)
    assert "| крипта | t48 | 24 | 6 |" in out and "| все | t48 | 24 | 6 |" in out
    assert "quant+LLM" in out and "ROI" in out


# ─────────────────────────── сеть и данные ──────────────────────────
def test_series_hosts_whitelisted_get_only():
    for h in ("api.binance.com", "query1.finance.yahoo.com", "home.treasury.gov", "alfred.stlouisfed.org"):
        assert h in http.ALLOWED_HOSTS
    for url in ("https://fred.stlouisfed.org/graph/fredgraph.csv", "http://api.binance.com/api/v3/klines",
                "https://api.binance.com.evil.io/x"):
        with pytest.raises(http.ForbiddenRequest):
            http.get_text(url)


def test_series_data_lives_outside_repo(monkeypatch):
    from forecast_bot import paths

    monkeypatch.delenv("FORECAST_STATE_DIR")
    d = SB.data_dir().resolve()
    assert paths.main_checkout().resolve() not in d.parents and d.name == "series"


def test_vintage_is_day_before_t():
    assert Q.vintage_for(datetime(2026, 9, 11, 0, 30, tzinfo=UTC)) == date(2026, 9, 10)


def test_llm_refuses_wrong_app(tmp_path, monkeypatch, fake_llm):
    """Если приложение гарда не polymarket — стоп до вызова: иначе траты лягут мимо потолка этапа."""
    from forecast_bot import guarded_llm

    R, ms, rows = _write_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(guarded_llm, "APP", "forecast")
    with pytest.raises(RuntimeError, match="FORECAST_APP"):
        asyncio.run(R.run_llm(types.SimpleNamespace(limit=0, first_point="t48")))
    assert fake_llm.prompts == []


def test_report_excludes_micro_from_comparison():
    """Застывшая котировка (объём < $1k) не участвует в сравнении Brier — иначе «бьём» несуществующий рынок."""
    rows = [_row(i, f"c{i % 4}", 0.1, 0.5, 0, volume=50.0, family="DXY") for i in range(8)]
    rows += [_row(10 + i, f"d{i % 4}", 0.3, 0.3, i % 2, volume=5000.0, family="DXY") for i in range(8)]
    out = SB.report(rows, boot=50)
    assert "| DXY | t48 | 8 | 4 |" in out and "DXY t48 8" in out


def test_quant_sees_series_only_up_to_market_price_time(tmp_path, monkeypatch):
    """Цена рынка — последняя точка CLOB до t (на час–два раньше t). Бары между ней и t модели не видны:
    иначе у quant лишняя информация против рынка, и «край» — артефакт."""
    R, ms, rows = _write_fixture(tmp_path, monkeypatch)
    for r in rows:
        assert datetime.fromisoformat(r["t_info"]) < datetime.fromisoformat(r["t"])
    cache = tmp_path / "cache" / "binance_BTCUSDT.json"
    d = json.loads(cache.read_text())
    r48 = next(r for r in rows if r["point"] == "t48")  # t50 раньше t48: порча (цена, t48] его не касается
    t_info, t_max = (datetime.fromisoformat(r48[k]).timestamp() for k in ("t_info", "t"))
    d["close"] = [c * (5 if t_info < a <= t_max else 1) for a, c in zip(d["avail"], d["close"])]
    cache.write_text(json.dumps(d))
    rows2, _ = R.quant_rows(R.load_markets(), R.store())
    assert [r["p_quant"] for r in rows2] == [r["p_quant"] for r in rows]


def test_load_news_drops_articles_at_or_after_t(tmp_path, monkeypatch):
    R = _runner()
    monkeypatch.setattr(SB, "data_dir", lambda: tmp_path)
    t = datetime(2026, 9, 1, 16, tzinfo=UTC)
    arts = [{"seen": (t + timedelta(minutes=m)).isoformat(), "title": f"a{m}", "url": "u", "domain": "d"}
            for m in (-90, -1, 0, 30)]
    (tmp_path / "gdelt.jsonl").write_text(json.dumps({"key": "g|t48", "t": t.isoformat(), "articles": arts}) + "\n")
    assert [a.title for a in R.load_news()["g|t48"]] == ["a-90", "a-1"]
