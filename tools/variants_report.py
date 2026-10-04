"""Отчёт по замеру вариантов (блок 2.1) из журнала: $, непроверенные числа, расхождение, топ-3 спорных вопроса.

    .venv/bin/python tools/variants_report.py [--since-hours 6] > отчёт.md

Расстояние между прогнозами одного вопроса: binary |Δp|; numeric/discrete |Δмедианы| / (верх − низ);
multiple choice — total variation ½·Σ|Δp|. Все три в долях [0, 1].
"""
from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot import paths  # noqa: E402

ORDER = ["A", "B", "C", "D", "H", "S", "G"]
LABEL = {"A": "агент Opus 5.5 high", "B": "исследование Haiku → итог Opus high",
         "C": "исследование Haiku → итог Gemini Flash", "D": "шаблон Gemini Flash + AskNews",
         "H": "Haiku 4.5 → Opus high (эталон)", "S": "Sonnet 5.5 → Opus high", "G": "Gemini 3.8 Flash → Opus high"}


def summary_value(qtype: str, pred: str):
    data = json.loads(pred)
    if qtype == "BinaryQuestion":
        return ("p", float(data))
    if qtype == "MultipleChoiceQuestion":
        return ("mc", {o["option_name"]: float(o["probability"]) for o in data["predicted_options"]})
    pts = sorted((p["percentile"], p["value"]) for p in data["declared_percentiles"])
    med = pts[-1][1]
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if p1 <= 0.5 <= p2:
            med = v1 + (v2 - v1) * ((0.5 - p1) / (p2 - p1) if p2 > p1 else 0)
            break
    span = float(data.get("upper_bound", 1)) - float(data.get("lower_bound", 0)) or 1.0
    return ("num", (med, span))


def distance(a, b) -> float:
    kind = a[0]
    if kind == "p":
        return abs(a[1] - b[1])
    if kind == "mc":
        keys = set(a[1]) | set(b[1])
        return 0.5 * sum(abs(a[1].get(k, 0) - b[1].get(k, 0)) for k in keys)
    return abs(a[1][0] - b[1][0]) / a[1][1]


def show(v) -> str:
    if v[0] == "p":
        return f"{v[1]:.0%}"
    if v[0] == "mc":
        top = max(v[1].items(), key=lambda kv: kv[1])
        return f"{top[0][:18]} {top[1]:.0%}"
    return f"мед. {v[1][0]:.4g}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=float, default=6)
    ap.add_argument("--variants", default=None, help="только эти варианты, через запятую")
    ap.add_argument("--skip-mc", action="store_true",
                    help="без multiple choice (до fix/mc-options выбор варианта искажал LLM-парсер)")
    args = ap.parse_args()
    conn = sqlite3.connect(paths.journal_db())
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM forecasts WHERE variant IS NOT NULL AND created_at >= ? ORDER BY id",
                        (time.time() - args.since_hours * 3600,)).fetchall()
    keep = set(args.variants.split(",")) if args.variants else None
    latest: dict[tuple[str, int], sqlite3.Row] = {}
    for r in rows:
        if keep and r["variant"] not in keep:
            continue
        if args.skip_mc and r["question_type"] == "MultipleChoiceQuestion":
            continue
        latest[(r["variant"], r["question_id"])] = r  # последний прогон варианта по вопросу
    by_v = defaultdict(list)
    for (v, _), r in latest.items():
        by_v[v].append(r)

    out = ["| Вариант | вопросов ok | $ / вопрос | AskNews | чисел в исследовании | непроверено | "
           "чисел у прогнозиста | без опоры |", "|---|---|---|---|---|---|---|---|"]
    for v in ORDER:
        rs = [r for r in by_v.get(v, []) if r["status"] == "ok"]
        if not rs:
            continue
        cost = sum(r["cost_usd"] or 0 for r in rs) / len(rs)
        rn = sum(r["research_numbers"] or 0 for r in rs)
        ru = sum(r["research_unverified"] or 0 for r in rs)
        fn = sum(r["forecast_numbers"] or 0 for r in rs)
        fu = sum(r["forecast_unverified"] or 0 for r in rs)
        research = f"{ru} ({ru / rn:.0%})" if rn else "— (исследование = тексты AskNews)"
        out.append(f"| {v}: {LABEL[v]} | {len(rs)} | ${cost:.4f} | {sum(r['asknews_calls'] or 0 for r in rs)} | "
                   f"{rn or '—'} | {research} | {fn} | {fu} ({fu / fn:.0%}) |" if fn else
                   f"| {v}: {LABEL[v]} | {len(rs)} | ${cost:.4f} | {sum(r['asknews_calls'] or 0 for r in rs)} | "
                   f"{rn or '—'} | {research} | 0 | — |")

    per_q: dict[int, dict[str, tuple]] = defaultdict(dict)
    meta: dict[int, sqlite3.Row] = {}
    for (v, qid), r in latest.items():
        if r["status"] == "ok" and r["prediction"]:
            per_q[qid][v] = summary_value(r["question_type"], r["prediction"])
            meta[qid] = r
    pair = defaultdict(list)
    spread = {}
    for qid, vals in per_q.items():
        ds = []
        for a, b in itertools.combinations(sorted(vals), 2):
            d = distance(vals[a], vals[b])
            pair[(a, b)].append(d)
            ds.append(d)
        spread[qid] = max(ds) if ds else 0
    out += ["", "Среднее расхождение между вариантами (доли [0, 1]):", "",
            "| пара | среднее | вопросов |", "|---|---|---|"]
    for (a, b), ds in sorted(pair.items()):
        out.append(f"| {a}–{b} | {sum(ds) / len(ds):.3f} | {len(ds)} |")
    out += ["", "Все вопросы:", "", "| вопрос | тип | " + " | ".join(ORDER) + " | разброс |",
            "|---|---|" + "---|" * len(ORDER) + "---|"]
    for qid in sorted(per_q, key=lambda q: -spread[q]):
        r = meta[qid]
        cells = [show(per_q[qid][v]) if v in per_q[qid] else "—" for v in ORDER]
        out.append(f"| [{(r['title'] or '')[:60]}]({r['url']}) | {r['question_type'][:6]} | "
                   + " | ".join(cells) + f" | {spread[qid]:.2f} |")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
