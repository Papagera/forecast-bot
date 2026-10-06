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
import re
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
# Market Pulse (spot peer score: считается только прогноз на момент закрытия вопроса). Сезон раз в квартал,
# слаг market-pulse-YYqN; новый сезон подхватывается сам — без деплоя, включение — переменной репо
# FORECAST_TOURNAMENTS (например «fall,minibench,pulse»). Любой другой слаг/ID добавляется туда же через запятую.
PULSE = "pulse"
# Обновления для spot-турниров: прогноз освежается раз в сутки и обязательно в последние 12 ч до закрытия.
UPDATE_EVERY_S = 24 * 3600
FINAL_WINDOW_S = 12 * 3600
FINAL_MIN_AGE_S = 3 * 3600
# Песочница Metaculus для проверки бота (main.py шаблона, режим test_questions) — только для dry-run.
TEST_TOURNAMENT = "bot-testing-area"
ASKNEWS_MONTHLY_CAP = 900  # решение income 02.10.2026: при лимите AskNews 1k/мес
PER_QUESTION_DAY_CALLS = 40  # ~16 вызовов на вопрос (1 сводка + 5 прогнозов + 10 парсеров) + запас на повторы
# Сколько после окна цикла ещё можно НАЧАТЬ вопрос (агент ≈ 1–3 мин на вопрос): 2 мин подготовки job +
# 335 мин цикла + 5 мин + последний вопрос ≈ 345 < timeout 350 мин.
LOOP_GRACE_S = 300


def pulse_slugs(today: dt.date) -> list[str]:
    """Слаги Market Pulse вокруг даты: прошлый, текущий и следующий квартал (несуществующие — пропускаются)."""
    q = (today.month - 1) // 3 + 1
    out = []
    for dq in (-1, 0, 1):
        y, qq = today.year, q + dq
        if qq == 0:
            y, qq = y - 1, 4
        elif qq == 5:
            y, qq = y + 1, 1
        out.append(f"market-pulse-{y % 100:02d}q{qq}")
    return out


def expand_tournaments(names: list[str], today: Optional[dt.date] = None) -> tuple[list, set]:
    """Имена из CLI/переменной → (ID/слаги, множество spot-турниров с обновлениями)."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    ids, refresh = [], set()
    for n in (x.strip() for x in names if x.strip()):
        if n == PULSE:
            for slug in pulse_slugs(today):
                ids.append(slug)
                refresh.add(slug)
        elif n in TOURNAMENTS:
            ids.append(TOURNAMENTS[n])
        elif n == "test":
            ids.append(TEST_TOURNAMENT)
        else:
            ids.append(int(n) if n.isdigit() else n)
            if n.startswith("market-pulse-"):
                refresh.add(n)
    return ids, refresh


_TOURNAMENT_SEEN: dict[str, tuple[bool, float]] = {}
MISSING_TTL_S = 3600


def tournament_exists(slug: str, now: Optional[float] = None) -> bool:
    """Есть ли турнир (один лёгкий запрос). Нужен, потому что forecasting-tools на несуществующий слаг делает
    3 повтора с паузами — на двух будущих сезонах Market Pulse это минуты на каждом опросе цикла.
    Отрицательный ответ кэшируется на час; при сетевой ошибке считаем «есть» — пусть решает обычный путь."""
    import requests

    now = now or time.time()
    hit = _TOURNAMENT_SEEN.get(slug)
    if hit and (hit[0] or now - hit[1] < MISSING_TTL_S):
        return hit[0]
    try:
        r = requests.get(f"https://www.metaculus.com/api/projects/tournaments/{slug}/",
                         headers={"Authorization": f"Token {os.environ.get('METACULUS_TOKEN', '')}"}, timeout=20)
        exists = r.status_code != 404
    except Exception:
        return True
    _TOURNAMENT_SEEN[slug] = (exists, now)
    return exists


def needs_update(last_submit: Optional[float], close_ts: Optional[float], now: float) -> bool:
    """Spot-турнир: прогнозировать снова? Первый раз — да; потом раз в сутки; в последние 12 ч — раз в 3 ч."""
    if last_submit is None:
        return True
    age = now - last_submit
    if close_ts is not None and close_ts - now <= FINAL_WINDOW_S:
        return age >= FINAL_MIN_AGE_S
    return age >= UPDATE_EVERY_S


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
    research = env.get("FORECAST_RESEARCH", "asknews")
    needs_asknews = research in ("asknews", "asknews-latest") or (
        research == "agent" and env.get("FORECAST_SEARCH", "web") == "asknews")
    if needs_asknews and not (env.get("ASKNEWS_API_KEY") or (env.get("ASKNEWS_CLIENT_ID") and env.get("ASKNEWS_SECRET"))):
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


def _forecast_number_check(explanation: str, q: Any) -> tuple[int, int]:
    """Числа в рассуждении прогнозиста, которых нет ни в исследовании, ни в тексте вопроса."""
    from forecast_bot import verify

    marker = "# FORECASTS"
    i = explanation.find(marker)
    forecast_part = explanation[i:] if i >= 0 else explanation
    # Собственные оценки прогнозиста — не факты: строки ответа (Probability/Percentile/«вариант: NN%») не считаем.
    answer_line = re.compile(r"(Probability|Percentile|^\s*[^:\n]{1,80}:\s*-?\d+(\.\d+)?\s*%?\s*$)", re.I)
    forecast_part = "\n".join(line for line in forecast_part.splitlines() if not answer_line.search(line))
    corpus = (explanation[:i] if i >= 0 else "") + " ".join(
        str(x or "") for x in (q.question_text, q.resolution_criteria, q.fine_print, q.background_info,
                               getattr(q, "lower_bound", ""), getattr(q, "upper_bound", "")))
    return verify.unsupported_numbers(forecast_part, corpus)


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
    variant: Optional[str] = None,
    stop_at: Optional[float] = None,
    refresh: Optional[set] = None,
) -> RunResult:
    from forecast_bot import ai_guard, guarded_llm, journal as J

    ai_guard.LIMITS["per_user_day_calls"] = max(ai_guard.LIMITS["per_user_day_calls"], PER_QUESTION_DAY_CALLS)
    guarded_llm.install_sentinel()
    result = RunResult(run_id=run_id or uuid.uuid4().hex[:12], submit=submit)
    mode = "submit" if submit else "dry"
    model = getattr(bot.get_llm("default", "llm"), "model", None)
    done = 0

    refresh = refresh or set()
    for tournament in tournaments:
        if tournament in refresh and isinstance(tournament, str) and not tournament_exists(tournament):
            logger.info("сезона %s пока нет — пропускаем", tournament)
            continue
        try:
            questions = await asyncio.to_thread(client.get_all_open_questions_from_tournament, tournament)
        except Exception as exc:
            if tournament in refresh:  # сезона Market Pulse ещё нет (или уже нет) — это норма
                logger.info("турнир %s недоступен: %s", tournament, type(exc).__name__)
                continue
            raise
        for q in questions:
            if limit is not None and done >= limit:
                return result
            if stop_at is not None and time.time() > stop_at:
                # Окно цикла вышло: новый вопрос не начинаем, иначе job упрётся в timeout и не перезапустится.
                result.stopped_reason = "время цикла вышло"
                return result
            qid = q.id_of_question
            if tournament in refresh:
                # Spot-очки: важен прогноз на момент закрытия → обновляем по расписанию, «не дважды» не действует.
                close_ts = q.close_time.timestamp() if q.close_time else None
                if submit and not needs_update(journal.last_submitted_at(qid), close_ts, time.time()):
                    continue
            # «Не дважды»: флаг Metaculus по бот-аккаунту + свой журнал (только реальные отправки).
            elif submit and (q.already_forecasted or journal.already_submitted(qid)):
                continue

            base = dict(run_id=result.run_id, question_id=qid, post_id=q.id_of_post,
                        tournament=str(tournament), question_type=type(q).__name__,
                        title=q.question_text, url=q.page_url, mode=mode, model=model)

            per_q = getattr(bot, "asknews_calls_per_research", 0)
            if per_q and journal.asknews_calls_this_month() + per_q > asknews_cap:
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
                        asknews_calls=bot.asknews_calls.pop(qid, 0),
                        web_searches=getattr(bot, "web_searches", {}).pop(qid, 0),
                        **getattr(bot, "research_stats", {}).pop(qid, {}))
            if variant:
                base["variant"] = variant

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
                if is_credit_error(row["error"]):
                    # Кошелёк OpenRouter пуст: остальные вопросы опроса упадут так же. Опрос стоп без ретраев,
                    # следующий — по расписанию (отказ 402 бесплатный). Сигнал — в лог и в итог запуска.
                    logger.error("🔴 OpenRouter: кредиты исчерпаны (402) — опрос остановлен, ничего не отправлено")
                    result.stopped_reason = CREDITS_STOP
                    return result
                logger.warning("вопрос %s: ошибка %s", qid, row["error"][:300])
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

            fn, fu = _forecast_number_check(report.explanation, q)
            row = dict(base, status=J.OK, prediction=_prediction_json(report.prediction),
                       reasoning=report.explanation, forecast_numbers=fn, forecast_unverified=fu)
            if submit:
                await report.publish_report_to_metaculus(metaculus_client=client)
                row["submitted_at"] = time.time()
            journal.record(**row)
            row["readable"] = _readable(report.prediction)
            result.rows.append(row)
    return result


CREDITS_STOP = "OpenRouter: кредиты исчерпаны (402)"
_CREDIT_MARKERS = ("exceed your available credits", "insufficient credits", "in_flight_budget_exhausted",
                   '"code":402', "'code': 402")


def is_credit_error(text: str) -> bool:
    """402 OpenRouter: пустой кошелёк или исчерпан in-flight бюджет (живьём 06.10.2026:
    `{"error":{"message":"This request would exceed your available credits …","code":402, …in_flight_budget_exhausted}}`)."""
    low = (text or "").lower()
    return any(m.lower() in low for m in _CREDIT_MARKERS)


@dataclass
class LoopStats:
    started_at: float
    finished_at: float = 0.0
    polls: int = 0                  # опросов с обращением к Metaculus
    skipped_day_cap: int = 0        # опросов, пропущенных: суточный потолок уже выбран (по леджеру)
    errors: int = 0                 # опросов, упавших с исключением (цикл продолжается)
    forecasts: int = 0              # строк ok за цикл
    last_found_at: Optional[float] = None   # когда в последний раз нашёлся новый вопрос
    stop_reasons: list[str] = field(default_factory=list)
    question_errors: int = 0        # строк error (вопрос не спрогнозирован) — раньше в строке цикла не было видно
    credit_stops: int = 0           # опросов, остановленных пустым кошельком OpenRouter

    def line(self) -> str:
        last = (dt.datetime.fromtimestamp(self.last_found_at, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                if self.last_found_at else "—")
        alarm = (f"🔴 OpenRouter без кредитов: остановлено опросов {self.credit_stops}, прогнозы не идут. "
                 if self.credit_stops else "")
        return (f"{alarm}Цикл: опросов {self.polls}, пропущено по суточному потолку {self.skipped_day_cap}, "
                f"упавших опросов {self.errors}, ошибок по вопросам {self.question_errors}, "
                f"прогнозов {self.forecasts}, последний найденный вопрос {last}")

    def save(self, path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False))


async def poll_loop(*, run_once, duration_s: float, poll_s: float, run_budget: Optional[float],
                    clock=time.time, sleep=None, day_spent=None, day_cap=None) -> LoopStats:
    """Цикл опроса внутри одного job: прогон → пауза poll_s → … пока не выйдет duration_s.

    - Лимит прогона (`run_budget`) обнуляется на КАЖДОМ опросе — это прежние «$1 за прогон».
    - Суточный потолок проверяется по леджеру ПЕРЕД каждым опросом: выбран — опрос пропускается целиком
      (ни Metaculus, ни моделей), цикл ждёт следующего окна. Внутри опроса его же держит ai_guard на каждом вызове.
    - Исключение в опросе не роняет цикл: считается и логируется, следующий опрос по расписанию.
    - Новый опрос не начинается, если до конца окна меньше паузы — job должен успеть сохранить кэш.
    """
    from forecast_bot import ai_guard, guarded_llm

    sleep = sleep or asyncio.sleep
    day_spent = day_spent or (lambda: ai_guard.spent_today_app(guarded_llm.APP))
    cap = day_cap if day_cap is not None else (ai_guard.APP_LIMITS.get(guarded_llm.APP) or {}).get("day_usd")
    stats = LoopStats(started_at=clock())
    deadline = stats.started_at + duration_s
    while True:
        if cap is not None and day_spent() >= cap:
            stats.skipped_day_cap += 1
        else:
            guarded_llm.start_run(run_budget)
            stats.polls += 1
            try:
                res = await run_once()
                ok = res.count("ok")
                stats.forecasts += ok
                stats.question_errors += res.count("error")
                if res.stopped_reason == CREDITS_STOP:
                    stats.credit_stops += 1
                if res.rows:
                    stats.last_found_at = clock()
                if res.stopped_reason and res.stopped_reason not in stats.stop_reasons[-1:]:
                    stats.stop_reasons.append(res.stopped_reason)
            except Exception as exc:  # сеть/API — не повод бросать цикл
                stats.errors += 1
                logger.exception("опрос упал: %s", exc)
        if clock() + poll_s >= deadline:
            break
        await sleep(poll_s)
    stats.finished_at = clock()
    return stats


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
    ap.add_argument("--tournament", choices=["minibench", "fall", "both", "test"], default="minibench")
    ap.add_argument("--tournaments", default=None,
                    help="список через запятую: fall,minibench,pulse,<слаг или ID> (перекрывает --tournament)")
    ap.add_argument("--quant-hints", action="store_true", help="статистическая база по рядам (Market Pulse)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--model", default=None, help="основная модель (перекрывает FORECAST_MODEL)")
    ap.add_argument("--predictions", type=int, default=None, help="прогнозов на вопрос (перекрывает FORECAST_PREDICTIONS)")
    ap.add_argument("--search", choices=["web", "asknews", "none"], default=None,
                    help="поиск агента: web (OpenRouter + Exa, по умолчанию) / asknews (выключен) / none")
    ap.add_argument("--agent-model", default=None, help="модель агента-исследователя (перекрывает FORECAST_AGENT_MODEL)")
    ap.add_argument("--agent-max-news", type=int, default=None, help="поисков AskNews агенту на вопрос (по умолчанию 3)")
    ap.add_argument("--reasoning", choices=["low", "medium", "high"], default=None,
                    help="reasoning_effort основной модели (перекрывает FORECAST_REASONING)")
    ap.add_argument("--research", choices=["asknews", "asknews-latest", "online", "none", "agent"], default=None,
                    help="поиск: AskNews свежие+архив (6 вызовов) / только свежие (1) / OpenRouter :online / без поиска")
    ap.add_argument("--run-budget", type=float, default=None,
                    help="лимит $ на один прогон (Actions: 1.0); в цикле — на каждый опрос")
    ap.add_argument("--loop-minutes", type=float, default=None,
                    help="крутить цикл опроса столько минут (Actions: 335 при timeout job 350)")
    ap.add_argument("--poll-minutes", type=float, default=10, help="пауза между опросами в цикле")
    ap.add_argument("--report-dir", default=None, help="куда положить отчёт dry-run (по умолчанию _отчёты/ основного чекаута)")
    args = ap.parse_args(argv)
    if args.research:
        os.environ["FORECAST_RESEARCH"] = args.research
    if args.quant_hints:
        os.environ["FORECAST_QUANT_HINTS"] = "1"
    if args.reasoning:
        os.environ["FORECAST_REASONING"] = args.reasoning
    if args.search:
        os.environ["FORECAST_SEARCH"] = args.search
    if args.agent_model:
        os.environ["FORECAST_AGENT_MODEL"] = args.agent_model
    if args.agent_max_news is not None:
        os.environ["FORECAST_AGENT_MAX_NEWS"] = str(args.agent_max_news)
    if args.report_dir:
        os.environ["FORECAST_REPORTS_DIR"] = args.report_dir
    if args.model:
        os.environ["FORECAST_MODEL"] = args.model
    if args.predictions:
        os.environ["FORECAST_PREDICTIONS"] = str(args.predictions)

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

    if args.tournaments:
        names = [n for n in args.tournaments.split(",") if n.strip()]
    else:
        names = ["minibench", "fall"] if args.tournament == "both" else [args.tournament]
    if "test" in names and args.mode != "dry":
        print("bot-testing-area — только для dry-run. Прогон не начат.")
        return 4
    with run_lock() as acquired:
        if not acquired:
            print("Прошлый прогон ещё идёт — выходим.")
            return 0
        from forecasting_tools import MetaculusClient

        from forecast_bot import guarded_llm
        from forecast_bot.bot import ForecastBot
        from forecast_bot.journal import Journal

        client, bot, journal = MetaculusClient(), ForecastBot(), Journal(paths.journal_db())
        tournaments, refresh = expand_tournaments(names)

        if args.loop_minutes:
            hard_stop = time.time() + args.loop_minutes * 60 + LOOP_GRACE_S

            async def run_once() -> RunResult:
                ids, spot = expand_tournaments(names)  # слаг сезона Market Pulse пересчитывается на каждом опросе
                return await run(client=client, bot=bot, journal=journal, tournaments=ids,
                                 submit=submit, limit=args.limit, stop_at=hard_stop, refresh=spot)

            stats = asyncio.run(poll_loop(run_once=run_once, duration_s=args.loop_minutes * 60,
                                          poll_s=args.poll_minutes * 60, run_budget=args.run_budget))
            stats.save(paths.state_dir() / "loop.json")
            print(stats.line())
            return 0

        guarded_llm.start_run(args.run_budget)
        result = asyncio.run(run(client=client, bot=bot, journal=journal, tournaments=tournaments,
                                 submit=submit, limit=args.limit, refresh=refresh))
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
