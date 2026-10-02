"""Раннер: берёт открытые вопросы турнира, прогнозирует по одному, пишет журнал, (опц.) отправляет.

    .venv/bin/python -m forecast_bot.run --mode dry --tournament minibench --limit 5

Отправка — только при ДВУХ независимых ключах: `--mode submit` И `FORECAST_SUBMIT=1` в .env.
По умолчанию — dry: прогноз и рассуждение в журнал и отчёт, в Metaculus ничего не уходит.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import fcntl
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from forecast_bot import paths

logger = logging.getLogger("forecast_bot")

# Турниры (forecasting_tools/helpers/metaculus_client.py, 0.3.2): Fall 2026 FutureEval = 33121,
# MiniBench — слаг, сам переходит на новый двухнедельный раунд (текущий раунд = project 33125).
TOURNAMENTS = {"fall": 33121, "minibench": "minibench"}
ASKNEWS_MONTHLY_CAP = 900  # решение income 02.10.2026: при лимите AskNews 1k/мес
PER_QUESTION_DAY_CALLS = 40  # ~16 вызовов на вопрос (1 сводка + 5 прогнозов + 10 парсеров) + запас на повторы


def submit_allowed(mode: str, env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return mode == "submit" and env.get("FORECAST_SUBMIT", "").strip() == "1"


def load_env_file(path) -> None:
    """Подгрузить .env, не перетирая уже заданное окружение. Значения никуда не печатаются."""
    from dotenv import dotenv_values

    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


def missing_keys(env: Optional[dict] = None) -> list[str]:
    env = os.environ if env is None else env
    missing = [k for k in ("METACULUS_TOKEN", "OPENROUTER_API_KEY") if not env.get(k)]
    if not (env.get("ASKNEWS_API_KEY") or (env.get("ASKNEWS_CLIENT_ID") and env.get("ASKNEWS_SECRET"))):
        missing.append("ASKNEWS_API_KEY")
    return missing


@dataclass
class RunResult:
    run_id: str
    submit: bool
    rows: list[dict] = field(default_factory=list)
    stopped_reason: Optional[str] = None

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r["status"] == status)


def _prediction_json(prediction: Any) -> str:
    if hasattr(prediction, "model_dump"):
        return json.dumps(prediction.model_dump(mode="json"), ensure_ascii=False)
    return json.dumps(prediction, ensure_ascii=False, default=str)


def _readable(prediction: Any) -> str:
    from forecasting_tools.data_models.data_organizer import DataOrganizer

    try:
        return DataOrganizer.get_readable_prediction(prediction)
    except Exception:
        return _prediction_json(prediction)


async def run(
    *,
    client: Any,
    bot: Any,
    journal: Any,
    tournaments: Iterable[Any],
    submit: bool,
    limit: Optional[int] = None,
    run_id: Optional[str] = None,
    asknews_cap: int = ASKNEWS_MONTHLY_CAP,
) -> RunResult:
    from forecast_bot import ai_guard, guarded_llm, journal as J
    from forecast_bot.bot import ASKNEWS_CALLS_PER_RESEARCH

    ai_guard.LIMITS["per_user_day_calls"] = max(ai_guard.LIMITS["per_user_day_calls"], PER_QUESTION_DAY_CALLS)
    guarded_llm.install_sentinel()
    result = RunResult(run_id=run_id or uuid.uuid4().hex[:12], submit=submit)
    mode = "submit" if submit else "dry"
    model = getattr(bot.get_llm("default", "llm"), "model", None)
    done = 0

    for tournament in tournaments:
        questions = await asyncio.to_thread(client.get_all_open_questions_from_tournament, tournament)
        for q in questions:
            if limit is not None and done >= limit:
                return result
            qid = q.id_of_question
            # «Не дважды»: флаг Metaculus по бот-аккаунту + свой журнал (только реальные отправки).
            if submit and (q.already_forecasted or journal.already_submitted(qid)):
                continue

            base = dict(run_id=result.run_id, question_id=qid, post_id=q.id_of_post,
                        tournament=str(tournament), question_type=type(q).__name__,
                        title=q.question_text, url=q.page_url, mode=mode, model=model)

            if journal.asknews_calls_this_month() + ASKNEWS_CALLS_PER_RESEARCH > asknews_cap:
                row = dict(base, status=J.SKIPPED_ASKNEWS, error=f"AskNews: потолок {asknews_cap}/мес")
                journal.record(**row)
                result.rows.append(row)
                result.stopped_reason = row["error"]
                return result

            user = f"q{qid}"
            ledger_user = f"{guarded_llm.APP}:{user}"
            t0 = time.time()
            token = guarded_llm.CURRENT_USER.set(user)
            try:
                report = await bot.forecast_question(q, return_exceptions=True)
            finally:
                guarded_llm.CURRENT_USER.reset(token)
            done += 1

            cost, ledger_calls = ai_guard.spent_by_user(ledger_user, t0)
            guarded_calls = guarded_llm.GUARDED_CALLS.pop(user, 0)
            base.update(cost_usd=cost, llm_calls=ledger_calls,
                        asknews_calls=bot.asknews_calls.pop(qid, 0))

            budget_hit = guarded_llm.BUDGET_HITS.pop(user, None)
            if budget_hit:
                # Честный пропуск: ничего не отправляем (даже если часть прогнозов успела), прогон стоп.
                row = dict(base, status=J.SKIPPED_BUDGET, error=budget_hit)
                journal.record(**row)
                result.rows.append(row)
                result.stopped_reason = f"ai_guard: {budget_hit}"
                return result

            if isinstance(report, BaseException):
                row = dict(base, status=J.ERROR, error=f"{type(report).__name__}: {report}"[:2000])
                journal.record(**row)
                result.rows.append(row)
                continue

            if guarded_llm.UNGUARDED_ATTEMPTS or guarded_calls != ledger_calls:
                # Сторож: вызов мимо гарда или расхождение с леджером → не отправляем ничего.
                err = (f"сторож ai_guard: мимо гарда {list(guarded_llm.UNGUARDED_ATTEMPTS)}, "
                       f"через гард {guarded_calls}, в леджере {ledger_calls}")
                row = dict(base, status=J.ERROR, error=err, prediction=_prediction_json(report.prediction),
                           reasoning=report.explanation)
                journal.record(**row)
                result.rows.append(row)
                result.stopped_reason = err
                return result

            row = dict(base, status=J.OK, prediction=_prediction_json(report.prediction),
                       reasoning=report.explanation)
            if submit:
                await report.publish_report_to_metaculus(metaculus_client=client)
                row["submitted_at"] = time.time()
            journal.record(**row)
            row["readable"] = _readable(report.prediction)
            result.rows.append(row)
    return result


def write_dry_report(result: RunResult, path) -> None:
    lines = [f"# Dry-run {dt.date.today():%d.%m.%Y} · прогон {result.run_id}", ""]
    ok = [r for r in result.rows if r["status"] == "ok"]
    total = sum(r.get("cost_usd") or 0 for r in ok)
    lines.append(f"Вопросов: {len(result.rows)}, прогнозов: {len(ok)}, "
                 f"стоимость по леджеру ai_guard: ${total:.4f}"
                 + (f", в среднем ${total / len(ok):.4f} на прогноз" if ok else ""))
    if result.stopped_reason:
        lines.append(f"Остановлено: {result.stopped_reason}")
    lines.append("")
    for r in result.rows:
        lines += [f"## {r['title']}", f"{r['url']} · {r['question_type']} · {r['status']}",
                  f"Прогноз: {r.get('readable') or r.get('prediction') or '—'}",
                  f"Стоимость: ${r.get('cost_usd') or 0:.4f} · вызовов LLM {r.get('llm_calls')} · "
                  f"AskNews {r.get('asknews_calls')}", ""]
        if r.get("error"):
            lines += [f"Ошибка: {r['error']}", ""]
        if r.get("reasoning"):
            lines += ["<details><summary>Рассуждение</summary>", "", r["reasoning"], "", "</details>", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


@contextlib.contextmanager
def run_lock():
    state = paths.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    fh = open(state / "run.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="forecast_bot.run")
    ap.add_argument("--mode", choices=["dry", "submit"], default="dry")
    ap.add_argument("--tournament", choices=["minibench", "fall", "both"], default="minibench")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    load_env_file(paths.env_path())

    missing = missing_keys()
    if missing:
        print(f"Нет ключей в {paths.env_path()}: {', '.join(missing)}. Прогон не начат.")
        return 2
    submit = submit_allowed(args.mode)
    if args.mode == "submit" and not submit:
        print("Отправка запрещена: нужен FORECAST_SUBMIT=1 в .env (ставится после «да» income). Прогон не начат.")
        return 3

    names = ["minibench", "fall"] if args.tournament == "both" else [args.tournament]
    with run_lock() as acquired:
        if not acquired:
            print("Прошлый прогон ещё идёт — выходим.")
            return 0
        from forecasting_tools import MetaculusClient

        from forecast_bot.bot import ForecastBot
        from forecast_bot.journal import Journal

        result = asyncio.run(run(client=MetaculusClient(), bot=ForecastBot(), journal=Journal(paths.journal_db()),
                                 tournaments=[TOURNAMENTS[n] for n in names], submit=submit, limit=args.limit))
    if not submit:
        report_path = paths.reports_dir() / f"dry-run-{dt.date.today():%Y-%m-%d}-{result.run_id}.md"
        write_dry_report(result, report_path)
        print(f"Отчёт dry-run: {report_path}")
    print(f"Прогон {result.run_id}: ok={result.count('ok')} error={result.count('error')} "
          f"skipped={len(result.rows) - result.count('ok') - result.count('error')}"
          + (f" · стоп: {result.stopped_reason}" if result.stopped_reason else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
