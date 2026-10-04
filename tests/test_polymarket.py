"""Polymarket (этап 3): только чтение, без утечки будущего, деньги и данные под контролем."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forecast_bot.polymarket import backtest as B, gdelt, http, markets as M

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def _gamma(**kw):
    m = {"id": "777", "question": "Will X happen by Aug 20?", "slug": "x-aug-20", "description": "Resolves Yes if X.",
         "startDate": "2026-08-01T00:00:00Z", "closedTime": "2026-08-20 12:00:00+00", "volumeNum": "5000",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]', "clobTokenIds": '["111", "222"]',
         "umaResolutionStatus": "resolved", "feeType": "politics_fees", "negRisk": False}
    m.update(kw)
    return m


# ─────────────────────────── только чтение ──────────────────────────
TRADING = re.compile(r"py_clob_client|eth_account|private_key|sign_typed_data|EIP712|/order\b|/orders\b|"
                     r"requests\.(post|put|delete|patch)\(|\.post\(", re.I)


def test_no_trading_code():
    """Ни строки, способной торговать: клиент CLOB, ключи, подписи, ордера, не-GET запросы."""
    files = list((ROOT / "forecast_bot" / "polymarket").glob("*.py")) + list((ROOT / "tools").glob("polymarket*.py"))
    assert files
    hits = [f"{f.name}:{i}" for f in files for i, line in enumerate(f.read_text().splitlines(), 1)
            if TRADING.search(line) and not line.lstrip().startswith("#")]
    assert hits == []
    lock = (ROOT / "requirements.lock.txt").read_text().lower()
    assert "py-clob-client" not in lock and "eth-account" not in lock


def test_http_allows_only_get_to_whitelist():
    for url in ("https://example.com/x", "http://gamma-api.polymarket.com/markets", "https://clob.polymarket.com.evil.io/"):
        with pytest.raises(http.ForbiddenRequest):
            http.get_json(url)
    assert http.ALLOWED_HOSTS == {"gamma-api.polymarket.com", "clob.polymarket.com", "api.gdeltproject.org"}


def test_no_asknews_in_polymarket():
    files = list((ROOT / "forecast_bot" / "polymarket").glob("*.py")) + list((ROOT / "tools").glob("polymarket*.py"))
    assert not [f.name for f in files if re.search(r"asknews", f.read_text(), re.I) and f.name != "__init__.py"]


# ─────────────────────────── выборка ────────────────────────────────
def test_from_gamma_accepts_clean_binary_and_reads_outcome_and_fee():
    m = M.from_gamma(_gamma())
    assert m and m.outcome == 1 and m.yes_token == "111" and m.fee_rate == 0.04 and m.life_days < 20
    assert M.from_gamma(_gamma(outcomePrices='["0", "1"]')).outcome == 0


@pytest.mark.parametrize("kw", [
    {"outcomes": '["A", "B"]'},                          # не Yes/No
    {"umaResolutionStatus": "proposed"},                 # итог не окончательный
    {"outcomePrices": '["0.5", "0.5"]'},                 # 50/50 — неоднозначный итог
    {"startDate": "2026-06-01T00:00:00Z"},               # жил дольше 30 дней
])
def test_from_gamma_rejects(kw):
    assert M.from_gamma(_gamma(**kw)) is None


def test_fee_rates_by_category():
    assert M.fee_rate("crypto_fees") == 0.07 and M.fee_rate("geopolitics_fees") == 0.0
    assert M.fee_rate("sports_fees") == 0.05 and M.fee_rate("anything", fees_enabled=False) == 0.0


# ─────────────────────────── без утечки ─────────────────────────────
def test_price_before_is_strict():
    m = M.from_gamma(_gamma())
    m.history = [(100, 0.2), (200, 0.4), (300, 0.9)]
    assert m.price_before(200) == 0.2 and m.price_before(201) == 0.4 and m.price_before(100) is None


def test_gdelt_cutoff_is_enforced_client_side():
    """Живой пример 05.10: enddatetime 15.08 00:00 вернул статьи, увиденные 15.08 20:00 и 16.08 00:00."""
    cutoff = datetime(2026, 8, 15, tzinfo=UTC)
    arts = [gdelt.Article(datetime(2026, 8, 16, tzinfo=UTC), "after", "u1", "d", "en"),
            gdelt.Article(datetime(2026, 8, 15, 20, tzinfo=UTC), "after2", "u2", "d", "en"),
            gdelt.Article(datetime(2026, 8, 14, 23, 59, tzinfo=UTC), "before", "u3", "d", "en")]
    assert [a.title for a in gdelt.strictly_before(arts, cutoff)] == ["before"]


def test_gdelt_search_filters_fake_leaky_response(monkeypatch):
    payload = {"articles": [{"seendate": "20260816T000000Z", "title": "leak", "url": "u", "domain": "d"},
                            {"seendate": "20260814T100000Z", "title": "ok", "url": "u2", "domain": "d2"}]}
    seen = {}
    monkeypatch.setattr(gdelt, "get_json", lambda url, params: seen.setdefault("p", params) and payload or payload)
    arts = gdelt.search("Will the Federal Reserve cut rates in August?", datetime(2026, 8, 15, tzinfo=UTC))
    assert [a.title for a in arts] == ["ok"]
    assert seen["p"]["enddatetime"] == "20260815000000" and "Federal" in seen["p"]["query"]
    text = gdelt.as_research(arts)
    assert "[S1]" in text and "ok" in text and "leak" not in text


def test_points_respect_lifetime():
    s, c = datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 11, tzinfo=UTC)
    pts = B.points(s, c)
    assert pts["t50"] == datetime(2026, 8, 6, tzinfo=UTC) and pts["t48"] == c - timedelta(hours=48)
    assert B.points(s, s + timedelta(hours=30)) == {}      # слишком короткий рынок — точек нет
    assert list(B.points(s, s + timedelta(hours=40))) == ["t50"]  # t50 = старт+16 ч (за 24 ч до закрытия), t48 нет


# ─────────────────────────── деньги и издержки ──────────────────────
def test_paper_trade_threshold_side_fee_and_pnl():
    assert B.paper_trade(0.55, 0.50, 1, 0.10, 0.02, 0.04) is None
    t = B.paper_trade(0.70, 0.50, 1, 0.10, 0.02, 0.04)
    assert t.side == "yes" and t.cost == pytest.approx(0.51) and t.fee == pytest.approx(0.04 * 0.51 * 0.49)
    assert t.pnl == pytest.approx(1 - 0.51 - 0.04 * 0.51 * 0.49)
    t2 = B.paper_trade(0.20, 0.50, 1, 0.10, 0.02, 0.0)
    assert t2.side == "no" and t2.pnl == pytest.approx(-0.51)


def test_stage_cap_from_ledger():
    from forecast_bot import ai_guard

    assert ai_guard.APP_LIMITS["polymarket"] == {"day_usd": 2.0}
    assert B.stage_budget_left() == pytest.approx(B.STAGE_CAP_USD)
    conn = ai_guard._conn()
    conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                 (B.STAGE_START.timestamp() + 60, "openrouter", "m", "polymarket:pm1-t50-none", 0, 0, 20.5))
    conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                 (B.STAGE_START.timestamp() - 60, "openrouter", "m", "polymarket:old", 0, 0, 99.0))
    conn.commit(); conn.close()
    assert B.stage_budget_left() < 0


def test_data_lives_outside_repo(monkeypatch):
    from forecast_bot import paths

    monkeypatch.delenv("FORECAST_STATE_DIR")
    d = B.data_dir().resolve()
    assert paths.main_checkout().resolve() not in d.parents and d.name == "polymarket"


def test_report_has_segments_and_roi():
    rows = [{"pre_cutoff": False, "segment": "tail", "point": "t50", "mode": "none", "p_bot": 0.8, "p_mkt": 0.5,
             "outcome": 1, "fee_rate": 0.04}] * 3
    out = B.report(rows)
    assert "| после | tail | t50 | none | 3 |" in out and "ROI после издержек" in out


# ─────────────────────────── прогноз на дату ────────────────────────
def test_forecaster_freezes_date_and_passes_research(fake_llm, monkeypatch):
    from forecast_bot.polymarket import forecaster

    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    m = M.from_gamma(_gamma())
    t = datetime(2026, 8, 10, 12, tzinfo=UTC)
    p, err = asyncio.run(forecaster.forecast(m, "t50", t, "[S1] 2026-08-09 · d · headline"))
    assert err == "" and p == pytest.approx(0.3)
    prompt = next(x for x in fake_llm.prompts if '"Probability: ZZ%"' in x)
    assert "Today is 2026-08-10" in prompt and "[S1] 2026-08-09" in prompt
    assert "0.5" not in prompt.split("Your research assistant says:")[1].split("Today is")[0]  # цены рынка нет
