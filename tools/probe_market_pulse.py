"""Разведка Market Pulse: описание турнира (правила, очки, регистрация) и устройство вопросов. Только чтение.

    .venv/bin/python tools/probe_market_pulse.py [slug] [--dump]
"""
from __future__ import annotations

import json
import pathlib
import sys

import requests

BASE = "https://www.metaculus.com/api"
H = {"Authorization": "Token " + pathlib.Path.home().joinpath(".config/income/metaculus_token").read_text().strip()}


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "market-pulse-26q3"
    out = pathlib.Path("/tmp") / f"{slug}.json"
    r = requests.get(f"{BASE}/projects/tournaments/{slug}/", headers=H, timeout=60)
    print("tournament", slug, r.status_code)
    if r.ok:
        t = r.json()
        keep = {k: t.get(k) for k in ("id", "name", "slug", "type", "start_date", "close_date",
                                       "forecasting_end_date", "prize_pool", "bot_leaderboard_status",
                                       "score_type", "default_permission", "is_ongoing", "forecasts_count",
                                       "forecasters_count", "questions_count", "user_permission",
                                       "is_subscribed", "visibility")}
        print(json.dumps(keep, ensure_ascii=False, indent=1))
        print("ключи:", sorted(t))
        out.write_text(json.dumps(t, ensure_ascii=False))
        print("описание →", out, "символов:", len(t.get("description") or ""))
    r = requests.get(f"{BASE}/posts/", params={"tournaments": slug, "limit": 100, "with_cp": "true"},
                     headers=H, timeout=60)
    print("posts", r.status_code)
    if r.ok:
        res = r.json().get("results", [])
        print("постов на странице:", len(res))
        for p in res[:40]:
            q = p.get("question") or {}
            g = p.get("group_of_questions") or {}
            subs = g.get("questions") or []
            kind = q.get("type") or (f"group×{len(subs)}:" + ",".join(sorted({s.get('type', '?') for s in subs})))
            first = subs[0] if subs else q
            print(f"- {p['id']} [{kind}] {p.get('title', '')[:80]} | status {p.get('status') or q.get('status')} "
                  f"| open {(first.get('open_time') or '')[:10]} close {(first.get('scheduled_close_time') or '')[:10]} "
                  f"| score {first.get('default_score_type')} | labels {[s.get('label') for s in subs][:4]}")
        (pathlib.Path("/tmp") / f"{slug}-posts.json").write_text(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
