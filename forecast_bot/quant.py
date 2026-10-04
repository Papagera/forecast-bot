"""Количественный модуль (блок B): автоматические вопросы MiniBench по рыночным рядам.

Вопрос → спецификация (актив, тип условия, порог, дата) → ряд строго ДО даты открытия → вероятность/CDF
по эмпирике изменений ряда за горизонт вопроса (ядерная оценка в лог-доходностях).

Источники без ключа (Stooq с 10.2026 закрыт JS-проверкой браузера — не используем):
- Coinbase Exchange, дневные свечи по UTC (крипта: close = цена на конец суток UTC, как у CoinGecko/CMC);
- Yahoo Finance chart API (акции, индексы, валюты);
Неоднозначное (нет «close», товарные фьючерсы, потоки ETF, доминация, индексы CMC) — не разбирается вовсе:
лучше выкинуть вопрос, чем угадать правило резолва.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import requests

# ─────────────────────────── ряды ───────────────────────────────────
@dataclass
class Series:
    key: str
    dates: list[date]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    crypto: bool = False

    def before(self, d: date) -> "Series":
        """Только бары с датой строго раньше d — защита от утечки будущего."""
        n = sum(1 for x in self.dates if x < d)
        return Series(self.key, self.dates[:n], self.open[:n], self.high[:n], self.low[:n], self.close[:n], self.crypto)

    def on(self, d: date) -> Optional[int]:
        try:
            return self.dates.index(d)
        except ValueError:
            return None


UA = {"User-Agent": "Mozilla/5.0 forecast-bot research"}


def _yahoo(symbol: str) -> Series:
    p1 = int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"period1": p1, "period2": p2, "interval": "1d"}, headers=UA, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    tz = res["meta"].get("gmtoffset", 0)
    q = res["indicators"]["quote"][0]
    rows = []
    for i, ts in enumerate(res.get("timestamp") or []):
        vals = [q["open"][i], q["high"][i], q["low"][i], q["close"][i]]
        if None in vals:
            continue
        d = datetime.fromtimestamp(ts + tz, tz=timezone.utc).date()  # дата торгов по времени биржи
        rows.append((d, *map(float, vals)))
    return _series(f"yahoo:{symbol}", rows, crypto=False)


def _coinbase(product: str) -> Series:
    rows, start = {}, datetime(2018, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    while start < now:
        end = min(start + timedelta(days=299), now)
        r = requests.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                         params={"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()},
                         headers=UA, timeout=30)
        r.raise_for_status()
        for t, low, high, op, cl, _vol in r.json():
            rows[datetime.fromtimestamp(t, tz=timezone.utc).date()] = (float(op), float(high), float(low), float(cl))
        start = end + timedelta(days=1)
        time.sleep(0.35)
    return _series(f"coinbase:{product}", [(d, *v) for d, v in rows.items()], crypto=True)


def _fred(series_id: str) -> Series:
    """FRED без ключа: fredgraph.csv (дата, значение); пропуски «.» отбрасываются."""
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series_id},
                     headers=UA, timeout=30)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            x = float(v)
        except ValueError:
            continue
        rows.append((date.fromisoformat(d), x, x, x, x))
    return _series(f"fred:{series_id}", rows, crypto=False)


def _series(key: str, rows: list, crypto: bool) -> Series:
    rows = sorted(rows)
    return Series(key, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                  [r[3] for r in rows], [r[4] for r in rows], crypto)


def load(key: str, cache_dir: Path) -> Series:
    """`key` — "coinbase:BTC-USD" или "yahoo:AAPL"; кэш в cache_dir (JSON)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (re.sub(r"[^\w.=^-]", "_", key) + ".json")
    if path.exists():
        d = json.loads(path.read_text())
        return Series(d["key"], [date.fromisoformat(x) for x in d["dates"]], d["open"], d["high"], d["low"],
                      d["close"], d["crypto"])
    src, sym = key.split(":", 1)
    s = {"coinbase": _coinbase, "fred": _fred}.get(src, _yahoo)(sym)
    path.write_text(json.dumps({"key": s.key, "dates": [x.isoformat() for x in s.dates], "open": s.open,
                                "high": s.high, "low": s.low, "close": s.close, "crypto": s.crypto}))
    return s


# ─────────────────────────── разбор вопроса ─────────────────────────
CRYPTO = [(r"\bbitcoin\b|\bbtc\b", "BTC-USD"), (r"\bethereum\b|\beth\b", "ETH-USD"), (r"\bxrp\b", "XRP-USD"),
          (r"\bsolana\b|\bsol\b", "SOL-USD"), (r"\bdogecoin\b|\bdoge\b", "DOGE-USD"), (r"\bcardano\b", "ADA-USD")]
INDEX = [(r"s&p 500", "^GSPC"), (r"nasdaq[- ]100", "^NDX"), (r"nasdaq composite", "^IXIC"),
         (r"dow jones", "^DJI"), (r"russell 2000", "^RUT"), (r"\bvix\b", "^VIX"), (r"\bkospi\b", "^KS11")]
NAMES = {"lockheed martin": "LMT", "apple": "AAPL", "nvidia": "NVDA", "tesla": "TSLA", "microsoft": "MSFT",
         "amazon": "AMZN", "alphabet": "GOOGL", "netflix": "NFLX", "micron": "MU", "lululemon": "LULU"}
# Другой первоисточник (ставки, спреды, референсные курсы ЦБ, фьючерсы, потоки ETF, капитализация) или
# единицы, требующие пересчёта, — не берём: правило резолва нельзя воспроизвести по рыночному ряду надёжно.
EXCLUDE = (r"flow|inflow|outflow|dominance|season index|fear|greed|market cap|mnav|volume|gold|oil|crude|brent|"
           r"futures|yield|spread|reference rate|official|riesgo|country risk|in millions|ipo|first-day|first day|"
           r"merger|combination|listed|market capitalization|tokenized|ratio|eth/btc|larger percentage")
DATE_ISO = r"(\d{4})-(\d{2})-(\d{2})"
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
_MON = r"(january|february|march|april|may|june|july|august|september|sept|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.?"
DATE_RE = rf"{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?"
NUM = r"\$?\s?([\d][\d,]*(?:\.\d+)?)\s?(k|K)?"


def _month(tok: str) -> int:
    tok = tok.lower().rstrip(".")
    for name, i in MONTHS.items():
        if name.startswith(tok[:3]):
            return i
    raise ValueError(tok)


def _date(m: re.Match, default_year: int, base: int = 1) -> date:
    y = m.group(base + 2)
    return date(int(y) if y else default_year, _month(m.group(base)), int(m.group(base + 1)))


def _num(s: str, k: Optional[str]) -> float:
    v = float(s.replace(",", ""))
    return v * 1000 if k else v


@dataclass
class Spec:
    # close_above|close_below — закрытие в день target против порога;
    # any_above|any_below — внутридневной максимум/минимум в окне [start, target];
    # anyclose_above|anyclose_below — хоть одно закрытие в окне выше/ниже порога;
    # close_up — закрытие target выше закрытия ref; ath — закрытие target — новый рекорд; value — закрытие target.
    kind: str
    key: str                       # ключ ряда для load()
    target: date                   # дата закрытия (для окон — конец окна)
    start: Optional[date] = None   # начало окна
    threshold: Optional[float] = None
    ref: Optional[date] = None     # опорная дата close_up
    note: str = ""


def asset_key(text: str) -> Optional[str]:
    t = text.lower()
    m = re.search(r"\busd/([a-z]{3})\b", t)
    if m:
        return f"yahoo:{m.group(1).upper()}=X"
    m = re.search(r"\b(eur|gbp|aud)/usd\b", t)
    if m:
        return f"yahoo:{m.group(1).upper()}USD=X"
    for rx, sym in CRYPTO:
        if re.search(rx, t):
            return f"coinbase:{sym}"
    for rx, sym in INDEX:
        if re.search(rx, t):
            return f"yahoo:{sym}"
    m = re.search(r"\((?:NYSE|NASDAQ|Nasdaq|KRX|TSE|HKEX):\s?(\d{6})\)", text)
    if m and "KRX" in text:
        return f"yahoo:{m.group(1)}.KS"
    m = re.search(r"\((?:(?:NYSE|NASDAQ|Nasdaq):\s?)?([0-9A-Z]{1,6}(?:\.[A-Z]{1,2})?)\)", text)
    if m and not m.group(1).isdigit():
        return f"yahoo:{m.group(1)}"
    m = re.search(r"\b([A-Z]{2,5})'s market close price", text)  # автоматическая серия «TICKER's market close»
    if m:
        return f"yahoo:{m.group(1)}"
    for name, sym in NAMES.items():
        if name in t:
            return f"yahoo:{sym}"
    return None


def parse(title: str, qtype: str, year: int) -> Optional[Spec]:
    """Спецификация или None, если правило резолва неоднозначно/не про рыночный ряд."""
    t = title.lower()
    if re.search(EXCLUDE, t):
        return None
    key = asset_key(title)
    if key is None:
        return None
    if qtype == "binary":
        m = re.search(rf"market close price on {DATE_ISO} be higher than its market close price on {DATE_ISO}", t)
        if m:
            d2 = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            d1 = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
            return Spec("close_up", key, d2, ref=d1)
        m = re.search(rf"clos\w*[^?]*?\b(above|below|at or above|at or below)\s+{NUM}[^?]*?on any (?:trading )?day "
                      rf"(?:between|from) {DATE_RE}\s+(?:and|to|through|–|-)\s+{DATE_RE}", t)
        if m:
            d1 = date(int(m.group(6)) if m.group(6) else year, _month(m.group(4)), int(m.group(5)))
            d2 = date(int(m.group(9)) if m.group(9) else year, _month(m.group(7)), int(m.group(8)))
            side = "above" if "above" in m.group(1) else "below"
            return Spec(f"anyclose_{side}", key, d2, d1, _num(m.group(2), m.group(3)))
        m = re.search(rf"closing price (fall below|rise above|exceed) {NUM} on any day from {_MON}\s+(\d{{1,2}})\s*[–-]\s*(\d{{1,2}}),?\s+(\d{{4}})", t)
        if m:
            mon = _month(m.group(4))
            d1, d2 = date(int(m.group(7)), mon, int(m.group(5))), date(int(m.group(7)), mon, int(m.group(6)))
            side = "below" if "below" in m.group(1) else "above"
            return Spec(f"anyclose_{side}", key, d2, d1, _num(m.group(2), m.group(3)))
        m = re.search(rf"trade (above|below) {NUM} at any point between {DATE_RE} and {DATE_RE}", t)
        if m:
            d1 = date(int(m.group(6)) if m.group(6) else year, _month(m.group(4)), int(m.group(5)))
            d2 = date(int(m.group(9)) if m.group(9) else year, _month(m.group(7)), int(m.group(8)))
            return Spec(f"any_{m.group(1)}", key, d2, d1, _num(m.group(2), m.group(3)))
        m = re.search(rf"new all-time (?:record )?high on {DATE_RE}", t)
        if m and "close" in t:
            return Spec("ath", key, _date(m, year))
        m = re.search(rf"clos(?:e|ing price|e price)[^?]*?\b(above|below|exceed|over|under|at or above|at or below)"
                      rf"\s+[₩¥€£₦]?{NUM}[^?]*?\bon\s+(?:\w+day,\s+)?{DATE_RE}", t)
        if m and "any" not in t:
            side = "above" if any(w in m.group(1) for w in ("above", "exceed", "over")) else "below"
            return Spec(f"close_{side}", key, _date(m, year, base=4), threshold=_num(m.group(2), m.group(3)))
        return None
    if qtype in ("numeric", "discrete"):
        m = re.search(rf'value of "[^"]+" on {DATE_ISO}', t)  # автоматическая серия индексов: значение закрытия
        if m:
            return Spec("value", key, date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        m = re.search(rf"at the end of {DATE_RE}\s*\(utc\)", t)  # конец суток UTC = дневное закрытие крипты
        if m and key.startswith("coinbase:"):
            return Spec("value", key, _date(m, year))
        if "clos" not in t:
            return None  # «цена на дату» без закрытия — время суток не определено
        m = re.search(rf"\bon\s+(?:\w+day,\s+)?{DATE_RE}", t)
        if m and "on or before" not in t:
            return Spec("value", key, _date(m, year))
    return None


# ─────────────────────────── итог по ряду ───────────────────────────
NEAR_THRESHOLD = 0.004  # ближе 0.4% к порогу — разные источники могут разойтись, вопрос выкидываем


def resolve(spec: Spec, s: Series) -> tuple[Optional[float], str]:
    """(итог, причина-если-None). Binary → 1.0/0.0; value → число."""
    if spec.kind == "value":
        i = s.on(spec.target)
        return (s.close[i], "") if i is not None else (None, "нет бара на дату")
    if spec.kind in ("close_above", "close_below"):
        i = s.on(spec.target)
        if i is None:
            return None, "нет бара на дату"
        v = s.close[i]
        if abs(v / spec.threshold - 1) < NEAR_THRESHOLD:
            return None, "у порога"
        return float((v > spec.threshold) == (spec.kind == "close_above")), ""
    if spec.kind == "close_up":
        i, j = s.on(spec.target), s.on(spec.ref)
        if i is None or j is None:
            return None, "нет бара на дату"
        if abs(s.close[i] / s.close[j] - 1) < NEAR_THRESHOLD / 4:
            return None, "у порога"
        return float(s.close[i] > s.close[j]), ""
    if spec.kind.startswith("anyclose_"):
        idx = [i for i, d in enumerate(s.dates) if spec.start <= d <= spec.target]
        if not idx:
            return None, "нет баров в окне"
        ext = max(s.close[i] for i in idx) if spec.kind == "anyclose_above" else min(s.close[i] for i in idx)
        if abs(ext / spec.threshold - 1) < NEAR_THRESHOLD:
            return None, "у порога"
        return float(ext > spec.threshold) if spec.kind == "anyclose_above" else float(ext < spec.threshold), ""
    if spec.kind.startswith("any_"):
        idx = [i for i, d in enumerate(s.dates) if spec.start <= d <= spec.target]
        if not idx:
            return None, "нет баров в окне"
        if spec.kind == "any_above":
            ext = max(s.high[i] for i in idx)
            if abs(ext / spec.threshold - 1) < NEAR_THRESHOLD:
                return None, "у порога"
            return float(ext > spec.threshold), ""
        ext = min(s.low[i] for i in idx)
        if abs(ext / spec.threshold - 1) < NEAR_THRESHOLD:
            return None, "у порога"
        return float(ext < spec.threshold), ""
    if spec.kind == "ath":
        i = s.on(spec.target)
        if i is None or i == 0:
            return None, "нет бара на дату"
        prior = max(s.close[:i])
        if abs(s.close[i] / prior - 1) < NEAR_THRESHOLD:
            return None, "у порога"
        return float(s.close[i] > prior), ""
    return None, "неизвестный тип"


# ─────────────────────────── модель ─────────────────────────────────
LOOKBACK = 730   # баров истории для эмпирики
FLOOR = 0.02     # клип вероятностей 2–98% (ТЗ этапа 2, блок D)


def horizon(s: Series, last: date, target: date) -> int:
    """Число баров от последнего известного до целевой даты (крипта — календарные дни, остальное — будни)."""
    if s.crypto:
        return (target - last).days
    n, d = 0, last
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _returns(c: np.ndarray, h: int) -> np.ndarray:
    return np.log(c[h:] / c[:-h]) if len(c) > h else np.array([])


WIDE_SHARE, WIDE_MULT = 0.15, 3.0  # примесь широкого ядра — толстые хвосты (рынки чаще гауссовых «выбросов»)


def _kde_cdf(x: np.ndarray, samples: np.ndarray) -> np.ndarray:
    """CDF ядерной оценки в точках x: 85% гаусс с шириной по Сильверману + 15% того же ядра ×3 шире."""
    from math import erf

    sd = samples.std(ddof=1) if len(samples) > 1 else 0.05
    bw = max(1.06 * sd * len(samples) ** -0.2, 1e-4)
    phi = np.vectorize(lambda v: 0.5 * (1 + erf(v)))
    narrow = phi((x[:, None] - samples[None, :]) / (bw * math.sqrt(2))).mean(axis=1)
    wide = phi((x[:, None] - samples[None, :]) / (WIDE_MULT * bw * math.sqrt(2))).mean(axis=1)
    return (1 - WIDE_SHARE) * narrow + WIDE_SHARE * wide


@dataclass
class QuantForecast:
    prob: Optional[float] = None            # binary
    cdf: Optional[list[float]] = None       # value: CDF в точках grid
    grid: Optional[list[float]] = None
    s0: float = 0.0
    h: int = 0
    n: int = 0
    info: dict = field(default_factory=dict)


def forecast(spec: Spec, hist: Series, grid: Optional[list[float]] = None) -> Optional[QuantForecast]:
    """`hist` — ряд строго до даты открытия (Series.before)."""
    if len(hist.close) < 60:
        return None
    c = np.array(hist.close[-LOOKBACK:], dtype=float)
    s0, last = c[-1], hist.dates[-1]
    h = horizon(hist, last, spec.target)
    if h <= 0:
        return None
    r = _returns(c, h)
    if len(r) < 30:
        return None
    clip = lambda p: float(min(1 - FLOOR, max(FLOOR, p)))  # noqa: E731
    if spec.kind in ("close_above", "close_below"):
        p_above = 1 - _kde_cdf(np.array([math.log(spec.threshold / s0)]), r)[0]
        return QuantForecast(prob=clip(p_above if spec.kind == "close_above" else 1 - p_above), s0=s0, h=h, n=len(r))
    if spec.kind == "close_up":
        j = hist.on(spec.ref)
        if j is not None:  # опорное закрытие уже известно на дату открытия → это порог
            thr = hist.close[j]
            p_above = 1 - _kde_cdf(np.array([math.log(thr / s0)]), r)[0]
            return QuantForecast(prob=clip(p_above), s0=s0, h=h, n=len(r), info={"ref_known": True})
        if spec.ref < last:
            return None  # опорный день до открытия, но бара нет (выходной) — правило неясно
        h2 = horizon(hist, spec.ref, spec.target)
        r2 = _returns(c, h2) if h2 > 0 else np.array([])
        if len(r2) < 30:
            return None
        p_up = 1 - _kde_cdf(np.array([0.0]), r2)[0]
        return QuantForecast(prob=clip(p_up), s0=s0, h=h2, n=len(r2), info={"ref_known": False})
    if spec.kind in ("anyclose_above", "anyclose_below"):
        a = horizon(hist, last, spec.start - timedelta(days=1)) + 1 if spec.start > last else 1
        hits, total = 0, 0
        for i in range(len(c) - h):
            seg = c[i + a: i + h + 1] / c[i]
            if not len(seg):
                continue
            hits += (seg.max() > spec.threshold / s0) if spec.kind == "anyclose_above" else (seg.min() < spec.threshold / s0)
            total += 1
        return QuantForecast(prob=clip((hits + 1) / (total + 2)), s0=s0, h=h, n=total) if total else None
    if spec.kind in ("any_above", "any_below"):
        a = horizon(hist, last, spec.start - timedelta(days=1)) + 1 if spec.start > last else 1
        hi = np.array(hist.high[-LOOKBACK:]); lo = np.array(hist.low[-LOOKBACK:])
        hits, total = 0, 0
        for i in range(len(c) - h):
            seg = slice(i + a, i + h + 1)
            if spec.kind == "any_above":
                hits += hi[seg].max() / c[i] > spec.threshold / s0
            else:
                hits += lo[seg].min() / c[i] < spec.threshold / s0
            total += 1
        p = (hits + 1) / (total + 2)
        return QuantForecast(prob=clip(p), s0=s0, h=h, n=total)
    if spec.kind == "ath":
        ath_rel = max(hist.close) / s0
        hits, total = 0, 0
        for i in range(len(c) - h):
            path = c[i + 1: i + h + 1] / c[i]
            hits += path[-1] > max(ath_rel, path[:-1].max() if h > 1 else 0)
            total += 1
        return QuantForecast(prob=clip((hits + 1) / (total + 2)), s0=s0, h=h, n=total)
    if spec.kind == "value" and grid:
        g = np.array(grid, dtype=float)
        safe = np.where(g > 0, g, 1e-12)
        cdf = _kde_cdf(np.log(safe / s0), r)
        return QuantForecast(cdf=[float(x) for x in cdf], grid=grid, s0=s0, h=h, n=len(r))
    return None


# ─────────────────────────── счёт ───────────────────────────────────
def binary_scores(p: float, y: float) -> dict:
    """log score (ln), Brier, baseline-счёт Metaculus 100·log2(p_исхода/0.5)."""
    po = p if y == 1 else 1 - p
    return {"log": math.log(po), "brier": (p - y) ** 2, "baseline": 100 * math.log2(po / 0.5)}


def continuous_scores(cdf: list[float], grid: list[float], outcome: float, mix_uniform: float = 0.02) -> dict:
    """Счёт по CDF на сетке: масса бина с исходом против равномерного распределения по диапазону.
    baseline = 100·log2(p_бина / p_равномерного_бина); исход вне диапазона — масса хвоста.
    Сетка `grid` — границы бинов (N+1 точек). К CDF подмешивается 2% равномерного (минимальная масса)."""
    n = len(grid) - 1
    lo, hi = grid[0], grid[-1]
    tail_lo, tail_hi = cdf[0], 1 - cdf[-1]
    inner = cdf[-1] - cdf[0]
    if outcome < lo:
        p, pu = tail_lo, 0.0
    elif outcome > hi:
        p, pu = tail_hi, 0.0
    else:
        k = min(n - 1, int((outcome - lo) / (hi - lo) * n))
        p, pu = cdf[k + 1] - cdf[k], 1.0 / n
    # смесь с равномерным по диапазону + страховочный хвост
    total_unif = 1.0
    if pu:
        p = (1 - mix_uniform) * p + mix_uniform * pu
    else:
        p = (1 - mix_uniform) * p + mix_uniform * 0.005
        pu = 0.005
    p = max(p, 1e-6)
    return {"log": math.log(p * n), "baseline": 100 * math.log2(p / pu), "inner_mass": inner}


# ─────────────────────────── Market Pulse ───────────────────────────
# Группы Market Pulse: подвопрос = двухнедельный период «Jul 27 - Aug 7». Тексты условий закрытых вопросов
# API не отдаёт, поэтому определения ниже — допущения по названию группы; они печатаются в подсказке прогнозисту.
PULSE = [
    # (regex по заголовку группы, тип, ряды, единицы, допущение)
    (r"ust 10y yield", "end", ("fred:DGS10",), "pp", "значение DGS10 (FRED) на последний день периода"),
    (r"high yield option-adjusted spread", "end", ("fred:BAMLH0A0HYM2",), "pp",
     "значение BAMLH0A0HYM2 (FRED) на последний день периода"),
    (r"maximum intraday value of the vix", "max", ("yahoo:^VIX",), "pt", "максимум дневных High ^VIX за период"),
    (r"nvidia's stock price returns exceed microsoft", "rel", ("yahoo:NVDA", "yahoo:MSFT"), "pp",
     "доходность NVDA минус MSFT за период, закрытие к закрытию, в п.п."),
    (r"nvidia's stock price returns exceed apple", "rel", ("yahoo:NVDA", "yahoo:AAPL"), "pp",
     "доходность NVDA минус AAPL за период, закрытие к закрытию, в п.п."),
    (r"nasdaq-100 futures total price returns exceed s&p 500 futures", "rel", ("yahoo:NQ=F", "yahoo:ES=F"), "pp",
     "доходность NQ=F минус ES=F за период, в п.п."),
    (r"gold futures total price returns exceed s&p 500 futures", "rel", ("yahoo:GC=F", "yahoo:ES=F"), "pp",
     "доходность GC=F минус ES=F за период, в п.п."),
    (r"crude oil futures total price returns exceed s&p 500 futures", "rel", ("yahoo:CL=F", "yahoo:ES=F"), "pp",
     "доходность CL=F минус ES=F за период, в п.п."),
]
PERIOD_RE = rf"{_MON}\s+(\d{{1,2}})\s*[-–]\s*{_MON}?\s*(\d{{1,2}})"
PCTS = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)


def period(label: str, year: int) -> Optional[tuple[date, date]]:
    m = re.search(PERIOD_RE, (label or "").lower())
    if not m:
        return None
    m1 = _month(m.group(1))
    m2 = _month(m.group(3)) if m.group(3) else m1
    d1, d2 = date(year, m1, int(m.group(2))), date(year, m2, int(m.group(4)))
    return (d1, d2) if d2 >= d1 else None


def _weekdays(a: date, b: date) -> int:
    """Будни в (a, b]."""
    return sum(1 for k in range(1, (b - a).days + 1) if (a + timedelta(days=k)).weekday() < 5)


@dataclass
class PulseQuant:
    kind: str
    unit: str
    assumption: str
    percentiles: dict[float, float]
    n: int
    asof: date

    def hint(self) -> str:
        pts = ", ".join(f"{int(p * 100)}%: {v:.3f}" for p, v in self.percentiles.items())
        return (f"STATISTICAL BASELINE (computed from the data series itself, history up to {self.asof}, "
                f"{self.n} historical windows of the same length). Percentiles in {self.unit}: {pts}. "
                f"Assumed definition: {self.assumption}. Use it as the base rate and adjust only for specific, "
                f"sourced reasons; keep the tails at least this wide.")


def pulse_quant(group_title: str, label: str, year: int, asof: date, cache_dir: Path) -> Optional[PulseQuant]:
    """Эмпирическое распределение исхода подвопроса Market Pulse по окнам той же длины (данные строго до asof)."""
    t = (group_title or "").lower()
    spec = next((p for p in PULSE if re.search(p[0], t)), None)
    per = period(label, year)
    if spec is None or per is None or per[0] <= asof:
        return None
    _, kind, keys, unit, assumption = spec
    start, end = per
    gap = _weekdays(asof, start - timedelta(days=1))          # от последних данных до начала периода
    length = _weekdays(start - timedelta(days=1), end)        # будни внутри периода
    series = [load(k, cache_dir).before(asof) for k in keys]
    if any(len(s.close) < 300 for s in series):
        return None
    if kind == "end":
        v = np.array(series[0].close[-1500:])
        h = gap + length
        samples = v[-1] + (v[h:] - v[:-h])                      # аддитивные изменения за горизонт
    elif kind == "max":
        s = series[0]
        c, hi = np.array(s.close[-1500:]), np.array(s.high[-1500:])
        samples = np.array([hi[i + gap + 1: i + gap + length + 1].max() / c[i] for i in range(len(c) - gap - length)])
        samples = samples * c[-1]
    else:  # rel: общие даты двух рядов
        a, b = series
        common = sorted(set(a.dates) & set(b.dates))[-1500:]
        ia = {d: i for i, d in enumerate(a.dates)}
        ib = {d: i for i, d in enumerate(b.dates)}
        ca = np.array([a.close[ia[d]] for d in common]); cb = np.array([b.close[ib[d]] for d in common])
        L = length
        samples = ((ca[L:] / ca[:-L]) - (cb[L:] / cb[:-L])) * 100
    if len(samples) < 100:
        return None
    pct = {p: float(np.quantile(samples, p)) for p in PCTS}
    return PulseQuant(kind, unit, assumption, pct, int(len(samples)), series[0].dates[-1])


def pulse_outcome(group_title: str, label: str, year: int, cache_dir: Path) -> Optional[float]:
    """Фактический исход подвопроса Market Pulse по тем же допущениям, что и pulse_quant (для сухих прогонов)."""
    t = (group_title or "").lower()
    spec = next((p for p in PULSE if re.search(p[0], t)), None)
    per = period(label, year)
    if spec is None or per is None:
        return None
    _, kind, keys, _unit, _ = spec
    start, end = per
    series = [load(k, cache_dir) for k in keys]
    if kind == "end":
        s = series[0]
        idx = [i for i, d in enumerate(s.dates) if d <= end]
        return s.close[idx[-1]] if idx and s.dates[idx[-1]] >= start else None
    if kind == "max":
        s = series[0]
        vals = [s.high[i] for i, d in enumerate(s.dates) if start <= d <= end]
        return max(vals) if vals else None
    a, b = series

    def ret(s: Series) -> Optional[float]:
        before = [i for i, d in enumerate(s.dates) if d < start]
        inside = [i for i, d in enumerate(s.dates) if start <= d <= end]
        if not before or not inside:
            return None
        return s.close[inside[-1]] / s.close[before[-1]] - 1

    ra, rb = ret(a), ret(b)
    return None if ra is None or rb is None else (ra - rb) * 100
