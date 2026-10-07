"""Тест «вероятность направления ETH»: всё строго до t — цена, базовые линии, ставка финансирования, вопрос боту."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

from forecast_bot import eth_direction as E

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _prices(days=500, spike_after=None):
    avail, close = [], []
    for h in range(days * 24):
        ts = T0 + timedelta(hours=h + 1)
        v = 100 + (h % 48) - 24 + h * 0.01                       # пила + слабый рост
        if spike_after is not None and ts > spike_after:
            v *= 10
        avail.append(ts.timestamp())
        close.append(v)
    return E.Prices(avail, close)


def test_price_at_exact_hour_close():
    p = _prices(5)
    t = T0 + timedelta(days=2)
    assert p.at(t) == p.close[47] and p.at(t + timedelta(minutes=30)) is None


def test_baselines_ignore_everything_after_t():
    t = T0 + timedelta(days=400)
    clean, spoiled = _prices(), _prices(spike_after=t)
    for h in E.HORIZONS:
        assert E.base_freq(clean, t, h) == E.base_freq(spoiled, t, h)
        f = lambda p: E.conditional(p, t, h, lambda s: E.momentum_sign(p, s, h), E.momentum_sign(p, t, h))  # noqa
        assert f(clean) == f(spoiled)


def test_funding_sign_strictly_before_t():
    t = T0 + timedelta(days=10)
    fu = [(t.timestamp() - 3600, -0.0001), (t.timestamp(), 0.0002), (t.timestamp() + 60, 0.0003)]
    assert E.funding_sign(fu, t) == -1                                  # ставка ровно в t — ещё не известна
    assert E.funding_sign([], t) is None


def test_paper_trade_thresholds_and_fee():
    assert E.paper_trade(0.55, 100, 110) is None and E.paper_trade(0.45, 100, 90) is None
    assert E.paper_trade(0.6, 100, 110).pnl == pytest.approx(0.1 - 0.001)
    assert E.paper_trade(0.4, 100, 110).pnl == pytest.approx(-0.1 - 0.001)


def test_holdout_is_last_30_days():
    last = datetime(2026, 10, 6, tzinfo=UTC)
    assert E.is_holdout(last - timedelta(days=29), last) and not E.is_holdout(last - timedelta(days=30), last)


def test_question_has_no_price_after_t():
    R = importlib.import_module("tools.eth_direction")
    t = T0 + timedelta(days=400)
    p = _prices(spike_after=t)
    r = {"t": t.isoformat(), "h": "7d", "x": p.at(t)}
    q, bg, crit = R.question_text(r, p)
    later = [f"{p.at(t + timedelta(days=k)):,.2f}" for k in range(1, 8)]
    assert not any(v in q + bg + crit for v in later)                    # ни одной цены после t
    assert f"{p.at(t - timedelta(days=1)):,.2f}" in bg and f"{p.at(t):,.2f}" in q
