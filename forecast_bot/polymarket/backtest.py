"""Бэктест A: бот против рыночной цены Polymarket на закрытых рынках, «как будто на дату t».

Точки: t50 — середина срока жизни (не позже чем за 24 ч до закрытия), t48 — за 48 ч до закрытия.
Режимы: none — без поиска; gdelt — заголовки GDELT строго до t. Цену рынка бот НЕ видит.
Деньги: приложение леджера `polymarket` ($2/сутки) + потолок этапа $20 от STAGE_START (ТЗ этапа 3).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from forecast_bot import paths

APP = "polymarket"
# Начало этапа — в UTC и с запасом: первые строки приложения polymarket в леджере — 2026-10-04 22:26 UTC (у Никиты уже
# 05.10 по Киеву). Прежнее значение 05.10 00:00 UTC не видело $4.8 этих трат — потолок этапа недосчитывал.
STAGE_START = datetime(2026, 10, 4, tzinfo=timezone.utc)
STAGE_CAP_USD = 20.0
BACKTEST_DAY_USD = 8.0  # суточный потолок приложения на время бэктеста A (income 05.10.2026); для B — $2 из ai_guard
CUTOFF = datetime(2026, 7, 1, tzinfo=timezone.utc)  # Opus 5.5: knowledge cutoff — июнь 2026 (platform.claude.com)
MICRO_MAX_VOLUME = 1_000.0     # ниже — «микро»: рынок почти не торговался, цена = котировка открытия, а не оценка
TAIL_MAX_VOLUME = 10_000.0     # хвост: $1k–$10k объёма (граница по распределению выборки, решение income 05.10.2026)
LIQUID_MIN_VOLUME = 250_000.0
SPREAD = {"micro": 0.05, "tail": 0.03, "mid": 0.02, "liquid": 0.01}  # исторического стакана API не даёт — ≈оценка, есть чувствительность
THRESHOLDS = (0.05, 0.10, 0.15)
P_CLIP = (0.01, 0.99)


def data_dir() -> Path:
    """Решения, журнал, выборка — ВНЕ репозитория (репо публичный)."""
    return paths.state_dir() / "polymarket"


def segment(volume: float) -> str:
    if volume < MICRO_MAX_VOLUME:
        return "micro"
    if volume < TAIL_MAX_VOLUME:
        return "tail"
    if volume >= LIQUID_MIN_VOLUME:
        return "liquid"
    return "mid"


def points(start: datetime, closed: datetime) -> dict[str, datetime]:
    out = {}
    t50 = min(start + (closed - start) / 2, closed - timedelta(hours=24))
    if t50 > start + timedelta(hours=12):
        out["t50"] = t50
    t48 = closed - timedelta(hours=48)
    if t48 > start + timedelta(hours=12):
        out["t48"] = t48
        if "t50" in out and abs(out["t50"] - t48) < timedelta(hours=12):
            del out["t50"]  # у рынков 3–4 дня обе точки — за двое суток до закрытия; дубль прогноза не нужен
    return out


def clip(p: float) -> float:
    return min(P_CLIP[1], max(P_CLIP[0], p))


def scores(p: float, y: int) -> dict:
    p = clip(p)
    po = p if y == 1 else 1 - p
    return {"log": math.log(po), "brier": (p - y) ** 2}


def blend(p_bot: float, p_mkt: float) -> float:
    lg = lambda x: math.log(clip(x) / (1 - clip(x)))  # noqa: E731
    z = (lg(p_bot) + lg(p_mkt)) / 2
    return 1 / (1 + math.exp(-z))


@dataclass
class Trade:
    side: str          # "yes" | "no"
    cost: float        # цена доли + половина спреда
    fee: float         # taker fee = feeRate × c × (1 − c) на одну долю
    pnl: float


def paper_trade(p_bot: float, p_mkt: float, y: int, thr: float, spread: float, fee_rate: float) -> Optional[Trade]:
    """Вход, если |p_бот − цена| ≥ порога: покупаем недооценённую сторону 1 долей и держим до резолва."""
    edge = p_bot - p_mkt
    if abs(edge) < thr:
        return None
    side = "yes" if edge > 0 else "no"
    c = min(0.999, (p_mkt if side == "yes" else 1 - p_mkt) + spread / 2)
    fee = fee_rate * c * (1 - c)
    win = (y == 1) == (side == "yes")
    return Trade(side, c, fee, (1.0 if win else 0.0) - c - fee)


def stage_spent() -> float:
    from forecast_bot import ai_guard

    return ai_guard.app_cost_since(APP, STAGE_START.timestamp())


def stage_budget_left() -> float:
    return STAGE_CAP_USD - stage_spent()


def load_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def dedupe(rows: list[dict]) -> list[dict]:
    """Убрать t50, если у того же рынка и режима есть t48 в пределах 12 ч (строки до правки points())."""
    t48 = {(r["market"], r["mode"]): datetime.fromisoformat(r["t"]) for r in rows if r["point"] == "t48"}
    out = []
    for r in rows:
        k = (r["market"], r["mode"])
        if r["point"] == "t50" and k in t48 and abs(datetime.fromisoformat(r["t"]) - t48[k]) < timedelta(hours=12):
            continue
        out.append(r)
    return out


def report(rows: Iterable[dict]) -> str:
    """Таблица «сегмент × точка × режим»: Brier/log бот vs рынок vs смесь; бумажная прибыль по порогам."""
    from collections import defaultdict

    rows = dedupe(list(rows))
    for r in rows:  # сегмент — по объёму на момент отчёта (границы сегментов могли поменяться после прогона)
        r["segment"] = segment(float(r.get("volume") or 0))
    groups = defaultdict(list)
    for r in rows:
        groups[(r["pre_cutoff"], r["segment"], r["point"], r["mode"])].append(r)
    out = ["| cutoff | сегмент | точка | поиск | n | Brier бот | Brier рынок | Brier смесь | log бот | log рынок |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(groups):
        rs = groups[key]
        b = lambda k: sum(scores(r[k], r["outcome"])["brier"] for r in rs) / len(rs)  # noqa: E731
        lg = lambda k: sum(scores(r[k], r["outcome"])["log"] for r in rs) / len(rs)  # noqa: E731
        for r in rs:
            r["p_blend"] = blend(r["p_bot"], r["p_mkt"])
        out.append(f"| {'до' if key[0] else 'после'} | {key[1]} | {key[2]} | {key[3]} | {len(rs)} | {b('p_bot'):.3f} | "
                   f"{b('p_mkt'):.3f} | {b('p_blend'):.3f} | {lg('p_bot'):.3f} | {lg('p_mkt'):.3f} |")
    out += ["", "| cutoff | сегмент | точка | поиск | порог | сделок | доля прибыльных | ROI после издержек |",
            "|---|---|---|---|---|---|---|---|"]
    for key in sorted(groups):
        rs = groups[key]
        for thr in THRESHOLDS:
            trades = [t for r in rs if (t := paper_trade(r["p_bot"], r["p_mkt"], r["outcome"], thr,
                                                          SPREAD[r["segment"]], r["fee_rate"]))]
            if not trades:
                continue
            spent = sum(t.cost + t.fee for t in trades)
            wins = sum(1 for t in trades if t.pnl > 0)
            out.append(f"| {'до' if key[0] else 'после'} | {key[1]} | {key[2]} | {key[3]} | {thr:.2f} | {len(trades)} | "
                       f"{wins / len(trades):.0%} | {sum(t.pnl for t in trades) / spent:+.1%} |")
    return "\n".join(out)
