"""Бэктест №2 «ряды данных»: деньги этапа, строки результата и отчёт (Brier, log, ROI после издержек, бутстрэп).

Деньги: приложение леджера `polymarket`, пользователи `pm2:*`; потолок этапа $20 с STAGE2_START (решение
06.10.2026), сутки — $8 на время прогона (как в 3A). Траты 3A (пользователи `pm<id>-…`) в потолок №2 не входят.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from forecast_bot.polymarket import backtest as B

STAGE2_START = datetime(2026, 10, 6, tzinfo=timezone.utc)
STAGE2_CAP_USD = 20.0
STAGE2_DAY_USD = 8.0
LEDGER_PREFIX = "pm2"
BOOT = 2000
SPREAD_SENS = (0.0, 1.0, 2.0)


def data_dir() -> Path:
    return B.data_dir() / "series"


def stage_spent() -> float:
    from forecast_bot import ai_guard

    return ai_guard.app_cost_since(f"{B.APP}:{LEDGER_PREFIX}", STAGE2_START.timestamp())


def stage_budget_left() -> float:
    return STAGE2_CAP_USD - stage_spent()


def blend(p: float, q: float) -> float:
    return B.blend(p, q)


def _brier(rows, k):
    return sum(B.scores(r[k], r["outcome"])["brier"] for r in rows) / len(rows)


def _log(rows, k):
    return sum(B.scores(r[k], r["outcome"])["log"] for r in rows) / len(rows)


def trades(rows: list[dict], k: str, thr: float, spread_mult: float = 1.0) -> list[B.Trade]:
    out = []
    for r in rows:
        seg = B.segment(float(r.get("volume") or 0))
        if seg == "micro":  # без торгов цена — застывшая котировка открытия: исполнить нечего (как в 3A)
            continue
        t = B.paper_trade(r[k], r["p_mkt"], r["outcome"], thr, B.SPREAD[seg] * spread_mult, r["fee_rate"])
        if t:
            out.append(t)
    return out


def roi(ts: list[B.Trade]) -> Optional[float]:
    spent = sum(t.cost + t.fee for t in ts)
    return sum(t.pnl for t in ts) / spent if spent > 0 else None


def bootstrap(rows: list[dict], stat: Callable[[list[dict]], Optional[float]], n: int = BOOT, seed: int = 7
              ) -> Optional[tuple[float, float]]:
    """90%-интервал статистики при пересэмплировании КЛАСТЕРОВ (страйки одного события/недели — не независимы)."""
    by = defaultdict(list)
    for r in rows:
        by[r["cluster"]].append(r)
    keys = sorted(by)
    if len(keys) < 2:
        return None
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        pick = rng.integers(0, len(keys), len(keys))
        sample = [r for i in pick for r in by[keys[i]]]
        v = stat(sample)
        if v is not None and not math.isnan(v):
            vals.append(v)
    if len(vals) < n // 2:
        return None
    return float(np.percentile(vals, 5)), float(np.percentile(vals, 95))


def _fmt_ci(ci) -> str:
    return f"[{ci[0]:+.3f}; {ci[1]:+.3f}]" if ci else "—"


def _fmt_ci_pct(ci) -> str:
    return f"[{ci[0]:+.0%}; {ci[1]:+.0%}]" if ci else "—"


def report(rows: Iterable[dict], boot: int = BOOT) -> str:
    rows = [r for r in rows if r.get("p_quant") is not None and r.get("p_mkt") is not None]
    for r in rows:
        r["p_blend"] = blend(r["p_quant"], r["p_mkt"])
    groups = defaultdict(list)
    for r in rows:
        groups[(r["family"], r["point"])].append(r)
        groups[("все", r["point"])].append(r)
    out = ["| класс | точка | n | кластеров | Brier quant | Brier quant+LLM | Brier рынок | Brier смесь q+р | "
           "log quant | log q+LLM | log рынок | ΔBrier quant−рынок, 90% | ΔBrier q+LLM−рынок, 90% |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(groups, key=lambda k: (k[0] == "все", k)):
        rs = groups[key]
        llm = [r for r in rs if r.get("p_llm") is not None]
        dq = bootstrap(rs, lambda s: _brier(s, "p_quant") - _brier(s, "p_mkt"), boot)
        dl = bootstrap(llm, lambda s: _brier(s, "p_llm") - _brier(s, "p_mkt"), boot) if llm else None
        b_llm = f"{_brier(llm, 'p_llm'):.3f} (n {len(llm)})" if llm else "—"
        l_llm = f"{_log(llm, 'p_llm'):.3f}" if llm else "—"
        out.append(f"| {key[0]} | {key[1]} | {len(rs)} | {len({r['cluster'] for r in rs})} | "
                   f"{_brier(rs, 'p_quant'):.3f} | {b_llm} | {_brier(rs, 'p_mkt'):.3f} | {_brier(rs, 'p_blend'):.3f} | "
                   f"{_log(rs, 'p_quant'):.3f} | {l_llm} | {_log(rs, 'p_mkt'):.3f} | {_fmt_ci(dq)} | {_fmt_ci(dl)} |")
    out += ["", "Сделки: 1 доля на сигнал |p − цена| ≥ порога, до резолва; цена + полспреда + taker fee "
            "(feeType Gamma); «микро» (<$1k) не торгуется. ROI = прибыль / вложено.", "",
            "| класс | точка | модель | порог | сделок | кластеров | прибыльных | ROI | ROI 90% | ROI спред×0 / ×2 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(groups, key=lambda k: (k[0] == "все", k)):
        rs = groups[key]
        for model in ("p_quant", "p_llm"):
            rm = [r for r in rs if r.get(model) is not None]
            if not rm:
                continue
            for thr in B.THRESHOLDS:
                ts = trades(rm, model, thr)
                if not ts:
                    continue
                traded = [r for r in rm if B.segment(float(r.get("volume") or 0)) != "micro"
                          and abs(r[model] - r["p_mkt"]) >= thr]
                ci = bootstrap(rm, lambda s: roi(trades(s, model, thr)), boot)
                sens = " / ".join(f"{roi(trades(rm, model, thr, m)):+.0%}" for m in (0.0, 2.0))
                wins = sum(1 for t in ts if t.pnl > 0)
                out.append(f"| {key[0]} | {key[1]} | {'quant' if model == 'p_quant' else 'quant+LLM'} | {thr:.2f} | "
                           f"{len(ts)} | {len({r['cluster'] for r in traded})} | {wins / len(ts):.0%} | "
                           f"{roi(ts):+.1%} | {_fmt_ci_pct(ci)} | {sens} |")
    return "\n".join(out)
