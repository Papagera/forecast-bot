"""Бэктест A Polymarket (ТЗ этапа 3). Только чтение публичных данных; ничего не торгует.

    .venv/bin/python tools/polymarket_backtest.py select [--end-min 2026-04-01] [--end-max 2026-10-01] [--per-segment 150]
    .venv/bin/python tools/polymarket_backtest.py forecast --mode none|gdelt [--points t50,t48] [--limit 20]
    .venv/bin/python tools/polymarket_backtest.py report

Данные — ~/.forecast-bot/polymarket/ (вне репо). ИИ — приложение леджера `polymarket` ($2/сутки) + потолок этапа $20.
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "polymarket"  # ДО импорта forecast_bot: потолок приложения читается при импорте

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import paths  # noqa: E402
from forecast_bot.polymarket import backtest as B, gdelt, markets as M  # noqa: E402


def cmd_select(a) -> int:
    out = B.data_dir()
    out.mkdir(parents=True, exist_ok=True)
    keep: dict[str, list[M.Market]] = {"tail": [], "mid": [], "liquid": []}
    seen = 0
    for raw in M.iter_closed(a.end_min, a.end_max):
        seen += 1
        m = M.from_gamma(raw)
        if m is None:
            continue
        seg = B.segment(m.volume)
        if len(keep[seg]) >= (a.per_segment if seg != "mid" else a.per_segment // 3):
            if all(len(v) >= (a.per_segment if k != "mid" else a.per_segment // 3) for k, v in keep.items()):
                break
            continue
        try:
            m.history = M.load_history(m)
        except Exception as exc:
            print(f"история {m.id}: {type(exc).__name__}")
            continue
        if len(m.history) < 3:
            continue
        keep[seg].append(m)
    path = out / "markets.jsonl"
    path.write_text("\n".join(m.to_json() for v in keep.values() for m in v) + "\n")
    print(f"просмотрено {seen}, отобрано: " + ", ".join(f"{k} {len(v)}" for k, v in keep.items()) + f" → {path}")
    vols = sorted(m.volume for v in keep.values() for m in v)
    if vols:
        q = lambda p: vols[int(p * (len(vols) - 1))]  # noqa: E731
        print(f"объём: p10 {q(.1):,.0f}  p50 {q(.5):,.0f}  p90 {q(.9):,.0f}")
    return 0


async def _forecast(a) -> int:
    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.polymarket import forecaster

    markets = [M.Market.from_json(l) for l in (B.data_dir() / "markets.jsonl").read_text().splitlines() if l.strip()]
    res_path = B.data_dir() / "backtest.jsonl"
    done = {(r["market"], r["point"], r["mode"]) for r in B.load_results(res_path)}
    pts = a.points.split(",")
    segs = set(a.segments.split(","))
    n, stats = 0, Counter()
    for m in markets:
        if B.segment(m.volume) not in segs:
            continue
        for point, t in B.points(m.start, m.closed).items():
            if point not in pts or (m.id, point, a.mode) in done:
                continue
            if a.limit and n >= a.limit:
                break
            p_mkt = m.price_before(t.timestamp())
            if p_mkt is None:
                stats["нет цены до t"] += 1
                continue
            if B.stage_budget_left() <= 0:
                print(f"потолок этапа ${B.STAGE_CAP_USD} исчерпан — стоп")
                return 0
            research = ""
            if a.mode == "gdelt":
                try:
                    research = gdelt.as_research(gdelt.search(m.question, t))
                except Exception as exc:
                    stats[f"gdelt: {type(exc).__name__}"] += 1
                    research = "News search was unavailable; forecast from the question text alone."
            user = f"pm{m.id}-{point}-{a.mode}"
            token = guarded_llm.CURRENT_USER.set(user)
            t_start = time.time()
            try:
                p_bot, err = await forecaster.forecast(m, point, t, research)
            finally:
                guarded_llm.CURRENT_USER.reset(token)
            cost, calls = ai_guard.spent_by_user(f"{B.APP}:{user}", t_start)
            n += 1
            if p_bot is None:
                stats["ошибка прогноза"] += 1
                print(f"{m.id} {point}: {err[:160]}")
                if guarded_llm.BUDGET_HITS.pop(user, None):
                    print("лимит ai_guard — стоп")
                    return 0
                continue
            rec = {"market": m.id, "question": m.question, "url": m.url, "segment": B.segment(m.volume),
                   "volume": m.volume, "point": point, "t": t.isoformat(), "mode": a.mode, "p_mkt": p_mkt,
                   "p_bot": p_bot, "outcome": m.outcome, "fee_rate": m.fee_rate, "neg_risk": m.neg_risk,
                   "pre_cutoff": t < B.CUTOFF, "cost_usd": cost, "llm_calls": calls}
            with res_path.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["ok"] += 1
            print(f"{m.id} {point} {a.mode}: бот {p_bot:.2f} рынок {p_mkt:.2f} итог {m.outcome} ${cost:.4f}")
    print("итог:", dict(stats), f"потрачено на этап ${B.stage_spent():.4f}")
    return 0


def cmd_report(_a) -> int:
    rows = B.load_results(B.data_dir() / "backtest.jsonl")
    print(f"строк: {len(rows)}; потрачено на этап ${B.stage_spent():.4f} из ${B.STAGE_CAP_USD}\n")
    print(B.report(rows))
    return 0


def main() -> int:
    from forecast_bot.run import load_env_file

    load_env_file(paths.env_path())
    os.environ.setdefault("FORECAST_RESEARCH", "none")
    os.environ.setdefault("FORECAST_MODEL", "openrouter/anthropic/claude-opus-5.5")
    os.environ.setdefault("FORECAST_REASONING", "high")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--end-min", default="2026-04-01T00:00:00Z")
    s.add_argument("--end-max", default="2026-10-01T00:00:00Z")
    s.add_argument("--per-segment", type=int, default=150)
    f = sub.add_parser("forecast")
    f.add_argument("--mode", choices=["none", "gdelt"], required=True)
    f.add_argument("--points", default="t50,t48")
    f.add_argument("--segments", default="tail,liquid,mid")
    f.add_argument("--limit", type=int, default=0)
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "select":
        return cmd_select(a)
    if a.cmd == "forecast":
        from forecast_bot import guarded_llm

        guarded_llm.start_run(min(2.0, B.stage_budget_left()))
        return asyncio.run(_forecast(a))
    return cmd_report(a)


if __name__ == "__main__":
    sys.exit(main())
