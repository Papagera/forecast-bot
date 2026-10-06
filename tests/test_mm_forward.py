"""Г, форвард-замер: формула наград, доля, заполнения, только чтение, без кошельков, 7 дней."""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc


@pytest.fixture
def F(tmp_path, monkeypatch):
    mod = importlib.import_module("tools.mm_forward")
    monkeypatch.setattr(mod, "DATA", tmp_path / "mm")
    monkeypatch.setattr(mod, "PAUSE_S", 0)
    return mod


def test_score_and_qmin(F):
    assert F.score(4.5, 0) == 1 and F.score(4.5, 4.5) == 0 and F.score(4.5, 2.25) == pytest.approx(0.25)
    assert F.score(4.5, 6) == 0 and F.score(0, 1) == 0
    assert F.q_min(9, 0, 0.5) == pytest.approx(3.0)                     # одна сторона при середине 0.1–0.9: /3
    assert F.q_min(9, 0, 0.95) == 0                                     # вне 0.1–0.9 — только двусторонне


def test_our_share_falls_with_competition_and_distance(F):
    alone = F.our_share(0.5, 0.01, 50, 4.5, (0, 0))
    crowded = F.our_share(0.5, 0.01, 50, 4.5, (500, 500))
    far = F.our_share(0.5, 0.04, 50, 4.5, (500, 500))
    assert alone == 1.0 and 0 < far < crowded < 1
    assert F.our_share(0.5, 0.05, 50, 4.5, (0, 0)) == 0.0               # вне max spread — не в зачёт


def test_book_q_counts_only_within_spread_and_min_size(F):
    yes = {"bids": [(0.48, 100), (0.40, 100), (0.49, 5)], "asks": [(0.52, 100)]}
    no = {"bids": [(0.49, 50)], "asks": [(0.53, 50)]}
    q1, q2 = F.book_q(yes, no, 0.5, 4.5, 20)
    assert q1 == pytest.approx(F.score(4.5, 2) * 100 + F.score(4.5, 3) * 50)   # 0.40 вне 4.5¢, 5 долей < min
    assert q2 == pytest.approx(F.score(4.5, 2) * 100 + F.score(4.5, 1) * 50)


def test_yes_trade_drops_wallet_and_mirrors_no(F):
    t = {"proxyWallet": "0xabc", "timestamp": 10, "side": "BUY", "price": 0.7, "size": 5, "asset": "no"}
    v = F.yes_trade(t, "yes")
    assert v == (10, "sell", pytest.approx(0.3), 5) and "0xabc" not in json.dumps(v)


def test_fills_and_collateral(F):
    tr = [(1, "buy", 0.56, 30), (2, "buy", 0.53, 30), (3, "sell", 0.44, 80)]
    fl = F.fills(tr, 0.5, 0.045, 50)
    assert [(f["we"], f["qty"]) for f in fl] == [("sell", 30), ("buy", 50)]
    assert F.collateral(0.5, 0.045, 50) == pytest.approx(50 * (0.455 + 0.455))


def test_get_refuses_other_hosts_and_http(F):
    for url in ("https://example.com/x", "http://clob.polymarket.com/book", "https://clob.polymarket.com.evil.io/"):
        with pytest.raises(ValueError):
            F.get(url)


def _fake_get(calls):
    def get(url, params=None, timeout=30):
        calls.append(url)
        if url.endswith("/events"):
            if params.get("offset"):
                return []
            return [{"id": 1, "title": "Will Ukraine target Moscow on...?", "markets": [
                {"id": "m1", "conditionId": "c1", "question": "Will Ukraine target Moscow on October 8?",
                 "clobTokenIds": '["y1", "n1"]', "endDate": "2026-10-08T23:59:00Z", "volumeNum": "100",
                 "acceptingOrders": True}]}]
        if "/markets/" in url:
            return {"rewards": {"min_size": 20, "max_spread": 4.5, "rates": [{"rewards_daily_rate": 50}]}}
        if url.endswith("/book"):
            return {"bids": [{"price": "0.30", "size": "40"}], "asks": [{"price": "0.40", "size": "40"}]}
        if "trades" in url:
            return [{"proxyWallet": "0xdeadbeef", "timestamp": int(datetime.now(UTC).timestamp()), "side": "BUY",
                     "price": "0.41", "size": "10", "asset": "y1"}]
        raise AssertionError(url)
    return get


def test_snap_writes_aggregates_without_wallets_and_stops_after_7_days(F, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(F, "get", _fake_get(calls))
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    F.snap(now)
    lines = (tmp_path / "mm" / "snap-20261007.jsonl").read_text().splitlines()
    rec = json.loads(lines[0])
    assert rec["core"] and rec["rewards"]["rate"] == 50 and rec["yes"]["bids"] == [[0.3, 40.0]]
    assert "deadbeef" not in (tmp_path / "mm" / "snap-20261007.jsonl").read_text()
    n = len(calls)
    F.snap(now + timedelta(days=7, minutes=1))
    assert len(calls) == n                                               # после 7 дней — ни одного запроса


def test_core_does_not_crowd_out_similar_rewarded(F, monkeypatch):
    core = [{"id": f"c{i}", "cid": f"c{i}", "end": f"2026-10-{i + 8:02d}", "title": "Will Russia target Kyiv on...?"}
            for i in range(60)]
    other = [{"id": f"o{i}", "cid": f"o{i}", "end": "2026-10-20", "title": "Moscow air traffic"} for i in range(15)]
    monkeypatch.setattr(F, "discover", lambda: (list(core), list(other)))
    monkeypatch.setattr(F, "rewards", lambda cid: {"min_size": 20, "max_spread": 4.5, "rate": 5.0})
    monkeypatch.setattr(F, "book", lambda tok: {"bids": [], "asks": []})
    monkeypatch.setattr(F, "get", lambda *a, **k: [])
    for m in core + other:
        m.update(tokens=["y", "n"], event="e", question="q")
    F.snap(datetime(2026, 10, 7, tzinfo=UTC))
    st = json.loads((F.DATA / "state.json").read_text())
    ids = [m["id"] for m in st["markets"]]
    assert sum(i.startswith("o") for i in ids) == F.OTHER_SLOTS and ids[0] == "c0" and len(ids) == F.MAX_MARKETS
