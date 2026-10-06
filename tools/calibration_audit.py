"""Аудит калибровки (этап 4, $0, без ИИ): перекос по корзинам вероятности и монотонная поправка.

    .venv/bin/python tools/calibration_audit.py [--out файл.md]

Источники (всё уже посчитано, новых вызовов нет):
- 3A (`polymarket/backtest.jsonl`): прогнозист Opus без/с GDELT, только после cutoff;
- №3 (`polymarket/ua/results.jsonl`): без новостей / GDELT / Telegram;
- №2 (`polymarket/series/{quant,llm}.jsonl`): quant и quant + LLM (рынки ≥ $1k);
- MiniBench: бинарные вопросы из журнала (dry, 02–05.10.2026) + итог из Metaculus API (только чтение).
Поправка — изотоническая (PAV) по прогнозам LLM-прогнозиста; подбор на июле–августе, проверка на сентябре и на
MiniBench (октябрь). Включать — только если улучшение держится на отложенных данных (ТЗ income 06.10.2026).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import paths  # noqa: E402
from forecast_bot.polymarket import backtest as B, series_backtest as SB  # noqa: E402

BINS = [i / 10 for i in range(11)]


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def month(ts: str) -> int:
    return datetime.fromisoformat(ts).month


def backtest_rows() -> list[dict]:
    d = B.data_dir()
    out = []
    for r in load_jsonl(d / "backtest.jsonl"):
        if r.get("pre_cutoff"):
            continue  # до cutoff модель могла знать ответ — не калибровка
        out.append({"src": f"3A·{r['mode']}", "kind": "llm", "p": r["p_bot"], "y": r["outcome"], "m": month(r["t"]),
                    "cluster": f"3a:{(r.get('closed') or r['market'])[:13]}"})
    for r in load_jsonl(d / "ua" / "results.jsonl"):
        out.append({"src": f"№3·{r['mode']}", "kind": "llm", "p": r["p_bot"], "y": r["outcome"], "m": month(r["t"]),
                    "cluster": r["cluster"]})
    q = {(r["market"], r["point"]): r for r in load_jsonl(d / "series" / "quant.jsonl") if SB.tradable(r)}
    for r in q.values():
        out.append({"src": "№2·quant", "kind": "quant", "p": r["p_quant"], "y": r["outcome"], "m": month(r["t"]),
                    "cluster": r["cluster"]})
    for rec in load_jsonl(d / "series" / "llm.jsonl"):
        for mid, p in (rec.get("p_llm") or {}).items():
            r = q.get((mid, rec["point"]))
            if r:
                out.append({"src": "№2·quant+LLM", "kind": "quant", "p": p, "y": r["outcome"], "m": month(r["t"]),
                            "cluster": r["cluster"]})
    return out


def minibench_rows() -> list[dict]:
    """Бинарные dry-прогнозы журнала + итог из Metaculus (кэш в каталоге данных)."""
    import requests

    from forecast_bot.run import load_env_file

    load_env_file(paths.env_path())
    db = paths.journal_db()
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("SELECT question_id, post_id, prediction, title FROM forecasts WHERE status='ok' AND "
                        "question_type='BinaryQuestion' AND mode='dry'").fetchall()
    conn.close()
    cache_p = B.data_dir() / "calib_metaculus.json"
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    tok = os.environ.get("METACULUS_TOKEN", "")
    out, seen = [], set()
    for qid, pid, pred, title in rows:
        if qid in seen:
            continue
        seen.add(qid)
        if str(pid) not in cache:
            r = requests.get(f"https://www.metaculus.com/api/posts/{pid}/", headers={"Authorization": f"Token {tok}"},
                             timeout=30)
            cache[str(pid)] = r.json() if r.ok else {}
        post = cache[str(pid)]
        qs = [post.get("question")] if post.get("question") else (post.get("group_of_questions") or {}).get(
            "questions", [])
        res = next((q.get("resolution") for q in qs if q and q.get("id") == qid), None)
        if res not in ("yes", "no"):
            continue
        out.append({"src": "MiniBench", "kind": "llm", "p": float(json.loads(pred)), "y": int(res == "yes"), "m": 10,
                    "cluster": f"mb:{pid}"})
    cache_p.write_text(json.dumps(cache))
    return out


def reliability(rows: list[dict]) -> list[str]:
    out = ["| корзина | n | средняя p | доля «Да» | перекос (p − частота) |", "|---|---|---|---|---|"]
    for lo, hi in zip(BINS, BINS[1:]):
        rs = [r for r in rows if lo <= r["p"] < hi or (hi == 1.0 and r["p"] == 1.0)]
        if not rs:
            continue
        mp, fy = sum(r["p"] for r in rs) / len(rs), sum(r["y"] for r in rs) / len(rs)
        out.append(f"| {lo:.1f}–{hi:.1f} | {len(rs)} | {mp:.3f} | {fy:.3f} | {mp - fy:+.3f} |")
    return out


def pav(points: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """Изотоническая регрессия (pool adjacent violators): [(p, y)] → ступени [(верх p, оценка)] неубывающие."""
    blocks = []  # [sum_y, n, max_p]
    for p, y in sorted(points):
        blocks.append([y, 1, p])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            y2, n2, p2 = blocks.pop()
            blocks[-1][0] += y2
            blocks[-1][1] += n2
            blocks[-1][2] = p2
    return [(b[2], b[0] / b[1]) for b in blocks]


def apply_pav(steps: list[tuple[float, float]], p: float) -> float:
    for top, v in steps:
        if p <= top:
            return min(0.99, max(0.01, v))
    return min(0.99, max(0.01, steps[-1][1]))


def brier(rows, key="p"):
    return sum((r[key] - r["y"]) ** 2 for r in rows) / len(rows)


def correction_check(train: list[dict], tests: dict[str, list[dict]], boot: int) -> list[str]:
    steps = pav([(r["p"], r["y"]) for r in train])
    out = [f"Поправка подобрана на {len(train)} прогнозах июля–августа ({len(steps)} ступеней).", "",
           "| проверка | n | кластеров | Brier до | Brier после | Δ (после − до), 90% | вывод |", "|---|---|---|---|---|---|---|"]
    for name, rs in tests.items():
        if not rs:
            out.append(f"| {name} | 0 | — | — | — | — | нет данных |")
            continue
        for r in rs:
            r["p_cal"] = apply_pav(steps, r["p"])
        ci = SB.bootstrap(rs, lambda s: brier(s, "p_cal") - brier(s), boot)
        ok = bool(ci and ci[1] < 0)
        out.append(f"| {name} | {len(rs)} | {len({r['cluster'] for r in rs})} | {brier(rs):.4f} | "
                   f"{brier(rs, 'p_cal'):.4f} | {'[%+.4f; %+.4f]' % ci if ci else '—'} | "
                   f"{'улучшает' if ok else 'не доказано'} |")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--no-metaculus", action="store_true")
    a = ap.parse_args()
    rows = backtest_rows()
    mb = [] if a.no_metaculus else minibench_rows()
    llm = [r for r in rows if r["kind"] == "llm"]
    lines = ["## Калибровка", "", f"Прогнозов: бэктесты {len(rows)} (LLM-прогнозист {len(llm)}), MiniBench бинарных с "
             f"итогом {len(mb)}.", "", "### LLM-прогнозист (3A + №3), все периоды", ""] + reliability(llm)
    by_src = defaultdict(list)
    for r in rows + mb:
        by_src[r["src"]].append(r)
    lines += ["", "| источник | n | Brier | средняя p | доля «Да» |", "|---|---|---|---|---|"]
    for s in sorted(by_src):
        rs = by_src[s]
        lines.append(f"| {s} | {len(rs)} | {brier(rs):.3f} | {sum(r['p'] for r in rs) / len(rs):.3f} | "
                     f"{sum(r['y'] for r in rs) / len(rs):.3f} |")
    if mb:
        lines += ["", "### MiniBench (бинарные, октябрь)", ""] + reliability(mb)
    lines += ["", "### Монотонная поправка LLM-прогнозиста", ""]
    lines += correction_check([r for r in llm if r["m"] in (7, 8)],
                              {"сентябрь (бэктесты)": [r for r in llm if r["m"] == 9], "MiniBench (октябрь)": mb},
                              a.boot)
    lines += ["", "Разброс 5 прогнозов и «сила справки» на этих данных проверить нельзя: бэктесты делали один прогноз, а "
              "журнал хранил только медиану. Данные начнёт собирать PR #17; проверка — после первых закрытых вопросов."]
    text = "\n".join(lines)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
