"""Ряды для бэктеста №2 «как будто на дату t». Каждый бар несёт момент, когда он стал ИЗВЕСТЕН (`avail`), и модель
видит только бары с `avail ≤ t` — это и есть защита от утечки будущего (`Bars.upto`).

- Binance 1h (крипта; источник резолва Polymarket): бар известен в момент закрытия = open + 1 ч.
- Yahoo 60m (DXY: DX-Y.NYB, WTI: CL=F — прокси Pyth): так же, open + 1 ч.
- Минфин США, Daily Treasury Par Yield Curve: ставка даты D публикуется ~18:00 ET → avail = D 23:00 UTC.
- ALFRED (макро): ряд в том виде, как он был опубликован на дату винтажа (первые публикации, без поздних ревизий).

Все запросы — через `polymarket.http` (белый список, только GET); кэш — в каталоге данных вне репозитория.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from forecast_bot.polymarket import http

UTC = timezone.utc
HOUR = 3600
TREASURY_PUBLISH_UTC_HOUR = 23  # ставка дня D известна не раньше D 23:00 UTC (публикация ~18:00 ET + запас)


@dataclass
class Bars:
    key: str
    avail: np.ndarray   # момент, когда бар стал известен (epoch, с)
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    diff: bool = False  # доходности: изменения в п.п., а не лог-доходности

    def upto(self, t: float) -> "Bars":
        """Только бары, известные к моменту t (avail ≤ t) — строго не из будущего."""
        n = int(np.searchsorted(self.avail, t, side="right"))
        return Bars(self.key, self.avail[:n], self.close[:n], self.high[:n], self.low[:n], self.diff)

    def between(self, t0: float, t1: float) -> "Bars":
        """Бары с t0 < avail ≤ t1 (для проверки «уже коснулось до t» и сверки резолва)."""
        a = self.avail
        i, j = np.searchsorted(a, t0, side="right"), np.searchsorted(a, t1, side="right")
        return Bars(self.key, a[i:j], self.close[i:j], self.high[i:j], self.low[i:j], self.diff)

    def __len__(self) -> int:
        return len(self.avail)


def _bars(key: str, rows: list[tuple[float, float, float, float]], diff: bool = False) -> Bars:
    rows = sorted({r[0]: r for r in rows}.values())
    a = np.array(rows, dtype=float).reshape(-1, 4)
    return Bars(key, a[:, 0], a[:, 1], a[:, 2], a[:, 3], diff)


def binance(symbol: str, start: datetime, end: datetime) -> Bars:
    rows, cur = [], int(start.timestamp() * 1000)
    stop = int(end.timestamp() * 1000)
    while cur < stop:
        k = http.get_json("https://api.binance.com/api/v3/klines",
                          {"symbol": symbol, "interval": "1h", "startTime": cur, "limit": 1000})
        if not k:
            break
        for r in k:  # [open_ms, open, high, low, close, ...]
            rows.append((r[0] / 1000 + HOUR, float(r[4]), float(r[2]), float(r[3])))
        cur = int(k[-1][0]) + HOUR * 1000
        if len(k) < 1000:
            break
    return _bars(f"binance:{symbol}", rows)


def binance_minute_close(symbol: str, minute: datetime) -> Optional[float]:
    """Закрытие 1-мин свечи Binance, открытой в `minute` (сверка резолва крипты «в 12:00 ET»)."""
    k = http.get_json("https://api.binance.com/api/v3/klines",
                      {"symbol": symbol, "interval": "1m", "startTime": int(minute.timestamp() * 1000), "limit": 1})
    if not k or int(k[0][0]) != int(minute.timestamp() * 1000):
        return None
    return float(k[0][4])


def yahoo(symbol: str) -> Bars:
    d = http.get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                      {"interval": "60m", "range": "730d"})
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c, h, lo = q["close"][i], q["high"][i], q["low"][i]
        if None in (c, h, lo):
            continue
        rows.append((ts + HOUR, float(c), float(h), float(lo)))
    return _bars(f"yahoo:{symbol}", rows)


def treasury(column: str, years: list[int]) -> Bars:
    rows = []
    for y in years:
        txt = http.get_text(
            f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all",
            {"type": "daily_treasury_yield_curve", "field_tdr_date_value": y, "_format": "csv"})
        for r in csv.DictReader(io.StringIO(txt)):
            v = (r.get(column) or "").strip()
            if not v:
                continue
            d = datetime.strptime(r["Date"], "%m/%d/%Y").replace(tzinfo=UTC)
            avail = d.replace(hour=TREASURY_PUBLISH_UTC_HOUR).timestamp()
            x = float(v)
            rows.append((avail, x, x, x))
    return _bars(f"treasury:{column}", rows, diff=True)


def alfred(series_id: str, vintage: date) -> list[tuple[date, float]]:
    """Ряд, каким он был опубликован на дату `vintage` (ALFRED alfredgraph.csv; без ключа, проверено 06.10.2026:
    JTSJOL на винтаж 2026-09-15 — июль 7271, в текущем ряду после ревизии 7335)."""
    txt = http.get_text("https://alfred.stlouisfed.org/graph/alfredgraph.csv",
                        {"id": series_id, "vintage_date": vintage.isoformat()})
    out = []
    for line in txt.splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            out.append((date.fromisoformat(d.strip()), float(v)))
        except ValueError:
            continue
    return out


# ─────────────────────────── кэш ───────────────────────────────────
class Store:
    """Загрузка и кэш рядов в каталоге данных (вне репозитория)."""

    def __init__(self, root: Path, start: datetime, end: datetime):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.start, self.end = start, end
        self._mem: dict[str, Bars] = {}

    def _path(self, name: str) -> Path:
        return self.root / (name.replace(":", "_").replace("/", "_").replace(" ", "_").replace("^", "") + ".json")

    def bars(self, key: str) -> Bars:
        if key in self._mem:
            return self._mem[key]
        p = self._path(key)
        if p.exists():
            d = json.loads(p.read_text())
            b = Bars(d["key"], np.array(d["avail"]), np.array(d["close"]), np.array(d["high"]), np.array(d["low"]),
                     d["diff"])
        else:
            src, sym = key.split(":", 1)
            if src == "binance":
                b = binance(sym, self.start, self.end)
            elif src == "yahoo":
                b = yahoo(sym)
            elif src == "treasury":
                b = treasury(sym, list(range(self.start.year, self.end.year + 1)))
            else:
                raise ValueError(key)
            p.write_text(json.dumps({"key": b.key, "avail": b.avail.tolist(), "close": b.close.tolist(),
                                     "high": b.high.tolist(), "low": b.low.tolist(), "diff": b.diff}))
        self._mem[key] = b
        return b

    def vintage(self, series_id: str, vintage: date) -> list[tuple[date, float]]:
        p = self._path(f"alfred:{series_id}@{vintage.isoformat()}")
        if p.exists():
            return [(date.fromisoformat(d), v) for d, v in json.loads(p.read_text())]
        rows = alfred(series_id, vintage)
        p.write_text(json.dumps([(d.isoformat(), v) for d, v in rows]))
        return rows
