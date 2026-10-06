"""Модель (a) бэктеста №2: вероятность условия рынка по истории ряда ДО t — без LLM, бесплатно.

Цены и ставки: s0 — последний известный бар; распределение изменения за горизонт H (от этого бара до момента
резолва) — по историческим окнам той же календарной длины за `LOOKBACK_DAYS` до t, только окнам, целиком известным
на t. Из каждого окна — итог (закрытие в конце), максимум High и минимум Low: так одной выборкой считаются и
«закроется выше K», и корзины, и «коснётся K». Ядерная оценка — `quant._kde_cdf` (толстые хвосты).

Макро (CPI, JOLTS): ALFRED-винтаж на день до t; MoM — эмпирика 36 последних месяцев; YoY — Монте-Карло: SA-MoM
+ сезонная поправка календарного месяца (5 лет), уровень JOLTS — сумма месячных изменений.

Поправка LLM (вариант b) — те же выборки, преобразованные: x' = vol_mult·x + shift_sigma·sd(итог)
(макро: вокруг среднего). Для максимума/минимума пути это приближение: сдвиг целиком к концу окна.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np

from forecast_bot.quant import FLOOR, _kde_cdf
from forecast_bot.polymarket.series import INF, Spec
from forecast_bot.polymarket.series_data import Bars

LOOKBACK_DAYS = {"hour": 365, "day": 3 * 365}
ANCHOR_STRIDE_S = {"hour": 3 * 3600, "day": 86400}  # окна-аналоги через 3 ч (часовые ряды), каждый день (дневные)
MAX_STALE_S = 4 * 86400   # последний известный бар старше 4 суток — ряд не годится (выходные DXY/WTI ≤ 2.5 сут)
MIN_ANALOGS = 60
MACRO_HIST = 36
MC_DRAWS = 4000


@dataclass
class Dist:
    s0: float
    diff: bool
    horizon_s: float
    final: np.ndarray
    mx: np.ndarray
    mn: np.ndarray
    info: dict = field(default_factory=dict)

    @property
    def sd(self) -> float:
        return float(self.final.std(ddof=1)) if len(self.final) > 1 else 0.0

    def x(self, level: float) -> float:
        """Порог в единицах выборки: лог-доходность (цены) или изменение в п.п. (ставки)."""
        if self.diff:
            return level - self.s0
        return float(np.log(level / self.s0)) if level > 0 else -INF


def _f(v: np.ndarray, base: float, diff: bool) -> np.ndarray:
    return v - base if diff else np.log(v / base)


def price_dist(bars: Bars, t: float, resolve_at: float, freq: str = "hour") -> Optional[Dist]:
    """Распределение изменений за горизонт от последнего известного на t бара до `resolve_at`."""
    known = bars.upto(t)
    if len(known) < MIN_ANALOGS * 2:
        return None
    a, c, h, lo = known.avail, known.close, known.high, known.low
    t_last = float(a[-1])
    if t - t_last > MAX_STALE_S:
        return None
    H = resolve_at - t_last
    if H <= 0:
        return None
    i0 = int(np.searchsorted(a, t_last - LOOKBACK_DAYS[freq] * 86400, side="left"))
    i1 = int(np.searchsorted(a, t_last - H, side="right"))  # окно аналога заканчивается не позже t_last
    final, mx, mn = [], [], []
    next_ok = -INF
    for i in range(i0, i1):
        if a[i] < next_ok:
            continue
        next_ok = a[i] + ANCHOR_STRIDE_S[freq]
        j = int(np.searchsorted(a, a[i] + H, side="right"))
        if j <= i + 1:
            continue
        # окно аналога (a[i], a[i] + H] целиком ≤ t_last ≤ t: из будущего ничего
        v = _f(np.array([c[j - 1], h[i + 1:j].max(), lo[i + 1:j].min()]), c[i], known.diff)
        final.append(v[0])
        mx.append(v[1])
        mn.append(v[2])
    if len(final) < MIN_ANALOGS:
        return None
    return Dist(float(c[-1]), known.diff, H, np.array(final), np.array(mx), np.array(mn),
                {"analogs": len(final), "t_last": t_last})


def _cdf(samples: np.ndarray, x: float) -> float:
    if x == INF:
        return 1.0
    if x == -INF:
        return 0.0
    return float(_kde_cdf(np.array([x]), samples)[0])


def clip(p: float) -> float:
    return float(min(1 - FLOOR, max(FLOOR, p)))


def adjust(samples: np.ndarray, sd: float, shift_sigma: float, vol_mult: float, center: float = 0.0) -> np.ndarray:
    return center + vol_mult * (samples - center) + shift_sigma * sd


def prob(spec: Spec, d: Dist, shift_sigma: float = 0.0, vol_mult: float = 1.0) -> Optional[float]:
    sd = d.sd
    final = adjust(d.final, sd, shift_sigma, vol_mult)
    if spec.kind == "close_above":
        return clip(1 - _cdf(final, d.x(spec.lo)))
    if spec.kind == "bracket":
        hi = INF if spec.hi == INF else d.x(spec.hi)
        lo = -INF if spec.lo == -INF else d.x(spec.lo)
        return clip(_cdf(final, hi) - _cdf(final, lo))
    if spec.kind in ("hit_high", "close_hit_high"):
        return clip(1 - _cdf(adjust(d.mx, sd, shift_sigma, vol_mult), d.x(spec.lo)))
    if spec.kind in ("hit_low", "close_hit_low"):
        return clip(_cdf(adjust(d.mn, sd, shift_sigma, vol_mult), d.x(spec.lo)))
    return None


def already_hit(spec: Spec, seen: Bars) -> bool:
    """Порог «коснётся» уже пройден на известных до t барах окна → рынок решён на t, точка не годится."""
    if not len(seen):
        return False
    if spec.kind in ("hit_high", "close_hit_high"):
        return bool(seen.high.max() >= spec.lo)
    if spec.kind == "hit_low":
        return bool(seen.low.min() <= spec.lo)
    if spec.kind == "close_hit_low":
        return bool(seen.low.min() < spec.lo)
    return False


# ─────────────────────────── макро ─────────────────────────────────
MACRO_SERIES = {  # ключ → (ряд в единицах резолва, SA-ряд для сезонности YoY, тип)
    "macro:cpi_mom": ("CPIAUCSL", None, "mom"),
    "macro:core_mom": ("CPILFESL", None, "mom"),
    "macro:cpi_yoy": ("CPIAUCNS", "CPIAUCSL", "yoy"),
    "macro:core_yoy": ("CPILFENS", "CPILFESL", "yoy"),
    "macro:jolts": ("JTSJOL", None, "level"),
}


def _mi(d: date) -> int:
    return d.year * 12 + d.month - 1


def _mom(rows: list[tuple[date, float]]) -> dict[int, float]:
    by = {_mi(d): v for d, v in rows}
    return {k: 100 * (v / by[k - 1] - 1) for k, v in by.items() if k - 1 in by}


@dataclass
class MacroDist:
    samples: np.ndarray
    center: float
    info: dict = field(default_factory=dict)

    @property
    def sd(self) -> float:
        return float(self.samples.std(ddof=1))


def vintage_for(t: datetime) -> date:
    """Винтаж ALFRED — на день раньше t: всё, что опубликовано в день t, считаем ещё неизвестным."""
    return (t - timedelta(days=1)).date()


def macro_dist(key: str, target_month: str, t: datetime, store, seed: int = 0) -> tuple[Optional[MacroDist], str]:
    """(распределение значения целевого месяца, причина-если-None)."""
    sid, sa_sid, typ = MACRO_SERIES[key]
    vint = vintage_for(t)
    rows = store.vintage(sid, vint)
    if not rows:
        return None, "пустой винтаж"
    y, m = map(int, target_month.split("-"))
    target = y * 12 + m - 1
    have = {_mi(d): v for d, v in rows}
    if target in have:
        return None, "значение уже опубликовано на t"
    last = max(have)
    steps = target - last
    if steps < 1 or steps > 3:
        return None, f"разрыв {steps} мес"
    rng = np.random.default_rng(seed)
    if typ == "mom":
        mom = _mom(rows)
        hist = np.array([mom[k] for k in sorted(mom)[-MACRO_HIST:]])
        return MacroDist(hist, float(hist.mean()), {"last": last, "steps": steps, "n": len(hist)}), ""
    if typ == "level":
        ch = np.array([have[k] - have[k - 1] for k in sorted(have)[-MACRO_HIST:] if k - 1 in have])
        draws = have[last] + rng.choice(ch, size=(MC_DRAWS, steps)).sum(axis=1)
        return MacroDist(draws, float(draws.mean()), {"last_value": have[last], "steps": steps}), ""
    # yoy: NSA-индекс целевого месяца = последний × Π(1 + SA-MoM + сезонность месяца)
    sa = store.vintage(sa_sid, vint)
    sa_mom, nsa_mom = _mom(sa), _mom(rows)
    hist = np.array([sa_mom[k] for k in sorted(sa_mom)[-MACRO_HIST:]])
    if target - 12 not in have:
        return None, "нет базы год назад"
    seas = {}
    for cal in range(12):
        diffs = [nsa_mom[k] - sa_mom[k] for k in nsa_mom if k in sa_mom and k % 12 == cal and k > last - 60]
        seas[cal] = float(np.mean(diffs)) if diffs else 0.0
    idx = np.full(MC_DRAWS, have[last])
    for k in range(last + 1, target + 1):
        idx = idx * (1 + (rng.choice(hist, size=MC_DRAWS) + seas[k % 12]) / 100)
    yoy = 100 * (idx / have[target - 12] - 1)
    return MacroDist(yoy, float(yoy.mean()), {"steps": steps, "base": have[target - 12]}), ""


def macro_prob(spec: Spec, d: MacroDist, shift_sigma: float = 0.0, vol_mult: float = 1.0) -> float:
    s = adjust(d.samples, d.sd, shift_sigma, vol_mult, center=d.center)
    return clip(_cdf(s, spec.hi) - _cdf(s, spec.lo))
