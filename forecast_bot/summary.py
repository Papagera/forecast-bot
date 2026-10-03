"""Итог запуска в Markdown (GitHub Step Summary): что спрогнозировано, статус, $ по леджеру.

    python -m forecast_bot.summary --since-minutes 30

Только данные журнала и леджера — ключей и окружения не касается.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from forecast_bot import ai_guard, paths
from forecast_bot.guarded_llm import APP
from forecast_bot.journal import Journal


def render(since: float) -> str:
    journal = Journal(paths.journal_db())
    rows = [r for r in journal.rows() if r["created_at"] >= since]
    run_cost = ai_guard.app_cost_since(APP, since)
    day_cost = ai_guard.spent_today_app(APP)
    sent = [r for r in rows if r["submitted_at"]]
    lines = [
        "## forecast-bot",
        f"Вопросов: {len(rows)} · отправлено: {len(sent)} · $ за запуск: {run_cost:.4f} · "
        f"$ за сутки (леджер): {day_cost:.4f}",
        "",
    ]
    loop_file = paths.state_dir() / "loop.json"
    if loop_file.exists():
        import json

        from forecast_bot.run import LoopStats

        try:
            data = json.loads(loop_file.read_text())
            if data.get("finished_at", 0) >= since:
                lines += [LoopStats(**data).line(), ""]
        except (ValueError, TypeError):
            lines += ["Цикл: файл статистики повреждён.", ""]
    if rows:
        lines += ["| вопрос | тип | статус | $ | AskNews |", "|---|---|---|---|---|"]
        for r in rows:
            title = (r["title"] or "").replace("|", "/")[:80]
            lines.append(f"| [{title}]({r['url']}) | {r['question_type']} | {r['status']} | "
                         f"{(r['cost_usd'] or 0):.4f} | {r['asknews_calls'] or 0} |")
    else:
        lines.append("Новых открытых вопросов нет.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="forecast_bot.summary")
    ap.add_argument("--since-minutes", type=float, default=30)
    args = ap.parse_args(argv)
    sys.stdout.write(render(time.time() - args.since_minutes * 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
