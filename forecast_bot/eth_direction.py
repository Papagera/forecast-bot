"""Тест «вероятность направления ETH» (08.10.2026): «ETH стоит X на t; P(цена через 24 ч / 7 дней выше X)?».

Точки t — 00:00 UTC ежедневно с 01.07.2026 (после cutoff Opus 5.5). Цена — Binance ETH/USDT, закрытие часовой свечи,
закрывшейся ровно в t (известна в момент t). Исход — закрытие часовой свечи в t + h выше X.
Базовые линии — только по данным СТРОГО до t (точки s с s + h ≤ t, чей исход известен на t):
- 50%;
- частота роста на том же горизонте за прошлые 90 дней;
- моментум: P(рост | знак доходности за 3 дня (24 ч) / 7 дней (7 д)) за прошлые 365 дней;
- ставка финансирования: P(рост | знак последней ставки Binance до t) за прошлые 365 дней.
Отложенная часть — последние 30 дней по t: ничего в ней не выбирается, отчёт отдельно.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc
HORIZONS = {"24h": 1, "7d": 7}
MOMENTUM_DAYS = {"24h": 3, "7d": 7}
FREQ_WINDOW_DAYS = 90
COND_WINDOW_DAYS = 365
HOLDOUT_DAYS = 30
FEE_ROUND_TRIP = 0.001
ENTER_HI, ENTER_LO = 0.6, 0.4


class Prices:
    """Закрытия часовых свечей по моменту закрытия (avail) — цена известна в момент avail."""

    def __init__(self, avail: list[float], close: list[float]):
        self.avail, self.close = list(avail), list(close)

    def at(self, t: datetime) -> Optional[float]:
        """Закрытие свечи, закрывшейся ровно в t (00:00 UTC — свеча 23:00–00:00)."""
        i = bisect_right(self.avail, t.timestamp()) - 1
        if i < 0 or abs(self.avail[i] - t.timestamp()) > 1:
            return None
        return self.close[i]


def outcome(p: Prices, t: datetime, horizon: str) -> Optional[int]:
    x, y = p.at(t), p.at(t + timedelta(days=HORIZONS[horizon]))
    if x is None or y is None:
        return None
    return int(y > x)


def daily_points(start: datetime, end: datetime) -> list[datetime]:
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(days=1)
    return out


def _known(p: Prices, t: datetime, horizon: str, window_days: int):
    """Точки s ∈ [t − window, t − h] (исход известен на t) с исходами."""
    h = HORIZONS[horizon]
    s, out = t - timedelta(days=window_days), []
    while s + timedelta(days=h) <= t:
        y = outcome(p, s, horizon)
        if y is not None:
            out.append((s, y))
        s += timedelta(days=1)
    return out


def base_freq(p: Prices, t: datetime, horizon: str) -> float:
    ys = [y for _, y in _known(p, t, horizon, FREQ_WINDOW_DAYS)]
    return (sum(ys) + 1) / (len(ys) + 2)


def momentum_sign(p: Prices, t: datetime, horizon: str) -> Optional[int]:
    x, past = p.at(t), p.at(t - timedelta(days=MOMENTUM_DAYS[horizon]))
    if x is None or past is None or x == past:
        return None
    return 1 if x > past else -1


def conditional(p: Prices, t: datetime, horizon: str, sign_at, sign_now: Optional[int]) -> float:
    """P(рост | тот же знак признака) по прошлым 365 дням; признак считается на каждой прошлой точке s."""
    if sign_now is None:
        return base_freq(p, t, horizon)
    ys = [y for s, y in _known(p, t, horizon, COND_WINDOW_DAYS) if sign_at(s) == sign_now]
    return (sum(ys) + 1) / (len(ys) + 2)


def funding_sign(funding: list[tuple[float, float]], t: datetime) -> Optional[int]:
    """Знак последней ставки финансирования с моментом СТРОГО до t. funding — [(epoch_s, rate)] по возрастанию."""
    i = bisect_right([f[0] for f in funding], t.timestamp() - 1e-6) - 1
    if i < 0:
        return None
    r = funding[i][1]
    return 1 if r > 0 else (-1 if r < 0 else None)


@dataclass
class PaperTrade:
    side: int       # +1 лонг, −1 шорт
    pnl: float      # доля от номинала после комиссии


def paper_trade(prob: float, x: float, y_price: float) -> Optional[PaperTrade]:
    """Вход только при p ≥ 0.6 (лонг) или p ≤ 0.4 (шорт); комиссия 0.1% туда-обратно."""
    if prob >= ENTER_HI:
        side = 1
    elif prob <= ENTER_LO:
        side = -1
    else:
        return None
    return PaperTrade(side, side * (y_price / x - 1) - FEE_ROUND_TRIP)


def is_holdout(t: datetime, last_t: datetime) -> bool:
    return t > last_t - timedelta(days=HOLDOUT_DAYS)


def log_score(p: float, y: int) -> float:
    p = min(0.99, max(0.01, p))
    return math.log(p if y else 1 - p)
