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
    errors = [r for r in rows if r["status"] == "error"]
    lines = [
        "## forecast-bot",
        f"Вопросов: {len(rows)} · отправлено: {len(sent)} · ошибок: {len(errors)} · $ за запуск: {run_cost:.4f} · "
        f"$ за сутки (леджер): {day_cost:.4f}",
        "",
    ]
    from forecast_bot.run import is_credit_error

    credit = sum(1 for r in errors if is_credit_error(r["error"] or ""))
    if credit:
        lines += [f"🔴 **OpenRouter: кредиты исчерпаны (402)** — {credit} попыток прогноза отклонено, ничего не "
                  "отправлено. Нужно пополнить баланс OpenRouter.", ""]
    elif errors:
        lines += [f"⚠ Последняя ошибка: {(errors[-1]['error'] or '')[:200]}", ""]
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
        # прогнозов / разброс — проверка калибровочного журнала (PR #17) прямо в итоге прогона: журнал живёт в кэше
        # Actions и снаружи не читается
        lines += ["| вопрос | тип | статус | $ | AskNews | прогнозов | разброс |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            title = (r["title"] or "").replace("|", "/")[:80]
            keys = r.keys()
            n = r["predictions_n"] if "predictions_n" in keys and r["predictions_n"] is not None else "—"
            sp = r["predictions_spread"] if "predictions_spread" in keys else None
            lines.append(f"| [{title}]({r['url']}) | {r['question_type']} | {r['status']} | "
                         f"{(r['cost_usd'] or 0):.4f} | {r['asknews_calls'] or 0} | {n} | "
                         f"{'—' if sp is None else f'{sp:.3f}'} |")
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
