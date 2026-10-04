"""Бэктест quant на прошлых раундах MiniBench: итог считаем сами по ряду, прогноз — по данным до открытия.

    .venv/bin/python tools/backtest_quant.py            # разбор + итог + quant + базовые линии
    → data/polygon/backtest_quant.jsonl, сводка в stdout

Только чтение открытых данных, без ИИ. LLM-часть — tools/backtest_llm.py по этому же файлу.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot import paths, quant as Q  # noqa: E402

CUTOFF = date(2026, 6, 30)  # Opus 5.5: reliable knowledge / training data cutoff — Jun 2026 (platform.claude.com)


def grid_for(row: dict):
    sc = row.get("scaling") or {}
    lo, hi, zp = sc.get("range_min"), sc.get("range_max"), sc.get("zero_point")
    if lo is None or hi is None or zp is not None:
        return None  # логарифмическая шкала — не берём, чтобы не ошибиться в бинах
    n = sc.get("inbound_outcome_count") or 200
    return [lo + (hi - lo) * k / n for k in range(n + 1)]


def main() -> int:
    rows = [json.loads(l) for l in (paths.data_dir() / "polygon" / "minibench.jsonl").open()]
    cache = paths.data_dir() / "polygon" / "series"
    out_path = paths.data_dir() / "polygon" / "backtest_quant.jsonl"
    reasons = Counter()
    results = []
    for row in rows:
        open_dt = datetime.fromisoformat(row["open_time"].replace("Z", "+00:00"))
        spec = Q.parse(row["title"], row["type"], open_dt.year)
        if spec is None:
            reasons["не разобран/неоднозначен"] += 1
            continue
        grid = grid_for(row) if spec.kind == "value" else None
        if spec.kind == "value" and grid is None:
            reasons["лог-шкала/нет диапазона"] += 1
            continue
        try:
            s = Q.load(spec.key, cache)
        except Exception as exc:
            reasons[f"ряд недоступен ({type(exc).__name__})"] += 1
            continue
        y, why = Q.resolve(spec, s)
        if y is None:
            reasons[f"итог: {why}"] += 1
            continue
        hist = s.before(open_dt.date())
        f = Q.forecast(spec, hist, grid)
        if f is None:
            reasons["мало истории"] += 1
            continue
        rec = {"post_id": row["post_id"], "question_id": row["question_id"], "round": row["round"],
               "title": row["title"], "type": row["type"], "kind": spec.kind, "key": spec.key,
               "open": open_dt.isoformat(), "target": spec.target.isoformat(),
               "threshold": spec.threshold, "outcome": y, "s0": f.s0, "h": f.h, "n": f.n,
               "pre_cutoff": spec.target <= CUTOFF}
        if f.prob is not None:
            rec["quant_p"] = f.prob
            rec["quant"] = Q.binary_scores(f.prob, y)
            rec["base"] = Q.binary_scores(0.5, y)
        else:
            rec["quant_cdf"] = f.cdf
            rec["grid"] = grid
            rec["quant"] = Q.continuous_scores(f.cdf, grid, y)
            uni = [k / (len(grid) - 1) for k in range(len(grid))]
            rec["base"] = Q.continuous_scores(uni, grid, y)
        results.append(rec)
        reasons["в бэктесте"] += 1

    out_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n")
    print("Разбор каталога:", dict(reasons))
    agg = defaultdict(list)
    for r in results:
        agg[("binary" if "quant_p" in r else "numeric", r["kind"])].append(r)
    print("\n| группа | n | quant baseline | 50%/равном. baseline | quant Brier | 50% Brier |")
    print("|---|---|---|---|---|---|")
    for (grp, kind), rs in sorted(agg.items()):
        qb = sum(r["quant"]["baseline"] for r in rs) / len(rs)
        bb = sum(r["base"]["baseline"] for r in rs) / len(rs)
        if grp == "binary":
            qbr = sum(r["quant"]["brier"] for r in rs) / len(rs)
            print(f"| {grp}:{kind} | {len(rs)} | {qb:+.1f} | {bb:+.1f} | {qbr:.3f} | 0.250 |")
        else:
            print(f"| {grp}:{kind} | {len(rs)} | {qb:+.1f} | {bb:+.1f} | — | — |")
    print(f"\nзаписано {len(results)} → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
