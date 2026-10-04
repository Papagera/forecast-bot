"""Просмотр каталога MiniBench по ключевому слову: заголовок, тип, окно, кусок критерия. Только чтение.

    .venv/bin/python tools/catalog_peek.py "regex" [--limit 80] [--criteria 220]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot import paths  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern")
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--criteria", type=int, default=220)
    a = ap.parse_args()
    rx = re.compile(a.pattern, re.I)
    rows = [json.loads(l) for l in (paths.data_dir() / "polygon" / "minibench.jsonl").open()]
    hits = [r for r in rows if rx.search(" ".join(str(r.get(k) or "") for k in ("title", "resolution_criteria")))]
    print(f"совпало {len(hits)} из {len(rows)}")
    for r in hits[: a.limit]:
        crit = re.sub(r"\s+", " ", r.get("resolution_criteria") or "")[: a.criteria]
        print(f"- [{r['type']}] {r['title']}\n    open {(r['open_time'] or '')[:16]} close {(r['close_time'] or '')[:16]}"
              f" resolve {(r['resolve_time'] or '')[:16]}\n    {crit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
