"""Каталог вопросов прошлых раундов MiniBench (тексты, тип, окно, критерий) → data/polygon/minibench.jsonl.

    .venv/bin/python tools/minibench_catalog.py [--rounds 12]

Раунды — каждые 2 недели, слаг `minibench-YYYY-MM-DD` (текущий 33125 = minibench-2026-09-21).
Итог (resolution) API этому аккаунту не отдаёт — его посчитаем сами по ряду (блок B). Только чтение.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import date, timedelta

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from forecast_bot import paths  # noqa: E402

BASE = "https://www.metaculus.com/api"
H = {"Authorization": "Token " + pathlib.Path.home().joinpath(".config/income/metaculus_token").read_text().strip()}
LAST_ROUND = date(2026, 9, 21)


def fetch_round(slug: str) -> list[dict]:
    out, offset = [], 0
    while True:
        for attempt in range(4):
            r = requests.get(f"{BASE}/posts/", params={"tournaments": slug, "limit": 100, "offset": offset,
                                                       "statuses": "closed,resolved"}, headers=H, timeout=60)
            if r.status_code != 429:
                break
            time.sleep(15 * (attempt + 1))
        if not r.ok:
            print(f"  {slug}: HTTP {r.status_code}")
            return out
        res = r.json().get("results", [])
        out += res
        if len(res) < 100:
            return out
        offset += 100


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=12)
    args = ap.parse_args()
    dst = paths.data_dir() / "polygon" / "minibench.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with dst.open("w", encoding="utf-8") as fh:
        for k in range(args.rounds):
            slug = f"minibench-{(LAST_ROUND - timedelta(days=14 * k)).isoformat()}"
            posts = fetch_round(slug)
            print(f"{slug}: {len(posts)}")
            for p in posts:
                q = p.get("question") or {}
                if not q:
                    continue
                fh.write(json.dumps({
                    "round": slug, "post_id": p["id"], "question_id": q.get("id"), "type": q.get("type"),
                    "title": q.get("title"), "resolution_criteria": q.get("resolution_criteria"),
                    "fine_print": q.get("fine_print"), "description": q.get("description"),
                    "open_time": q.get("open_time"), "close_time": q.get("scheduled_close_time"),
                    "resolve_time": q.get("scheduled_resolve_time"), "status": q.get("status"),
                    "scaling": q.get("scaling"), "options": q.get("options"),
                    "open_lower_bound": q.get("open_lower_bound"), "open_upper_bound": q.get("open_upper_bound"),
                    "resolution": q.get("resolution"),
                }, ensure_ascii=False) + "\n")
                n += 1
            time.sleep(2)
    print(f"записано {n} вопросов → {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
