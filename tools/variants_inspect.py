"""Ручной разбор спорного вопроса: исследование и хвост рассуждения прогнозиста по вариантам.

    .venv/bin/python tools/variants_inspect.py <post_id> [--variants A,B,D] [--chars 1500]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot import paths  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("post_id")
    ap.add_argument("--variants", default="A,B,D")
    ap.add_argument("--chars", type=int, default=1500)
    args = ap.parse_args()
    c = sqlite3.connect(paths.journal_db())
    c.row_factory = sqlite3.Row
    for v in args.variants.split(","):
        r = c.execute("SELECT * FROM forecasts WHERE variant = ? AND url LIKE ? ORDER BY id DESC LIMIT 1",
                      (v, f"%/{args.post_id}")).fetchone()
        if not r:
            print(f"===== {v}: нет строки")
            continue
        t = r["reasoning"] or ""
        i, j = t.find("# RESEARCH"), t.find("# FORECASTS")
        print(f"===== {v} · {r['title'][:90]} · {r['prediction'][:80]}")
        print("--- исследование:\n" + t[i:i + args.chars])
        if r["research_dropped"]:
            print("--- отброшено сверкой:\n" + r["research_dropped"][:600])
        tail = t[j:]
        print("--- конец рассуждения прогнозиста:\n" + tail[-args.chars:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
