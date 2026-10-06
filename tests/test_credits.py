"""Пустой кошелёк OpenRouter (402): цикл не падает, вопросы не пропускаются молча, ретраев нет."""
from __future__ import annotations

import asyncio
import time

from fakes import FakeMetaculusClient, questions

from forecast_bot import guarded_llm, run as R

# живой ответ OpenRouter 06.10.2026 (сокращён)
OPENROUTER_402 = ('litellm.APIError: APIError: OpenrouterException - {"error":{"message":"This request would exceed '
                  'your available credits given your current in-flight requests. Retry after in-flight requests settle, '
                  'or add credits.","code":402,"metadata":{"reason":"in_flight_budget_exhausted"}}}')


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def test_credit_error_stops_poll_loudly_and_sends_nothing(fake_llm, fake_asknews, caplog):
    from forecast_bot import paths
    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    fake_llm.raise_exc = RuntimeError(OPENROUTER_402)
    client = FakeMetaculusClient(questions())
    guarded_llm.start_run(1.0)
    with caplog.at_level("ERROR"):
        res = asyncio.run(R.run(client=client, bot=ForecastBot(), journal=Journal(paths.journal_db()),
                                tournaments=["t"], submit=True))
    assert res.stopped_reason == R.CREDITS_STOP
    assert [r["status"] for r in res.rows] == ["error"]           # первый вопрос — и стоп, без перебора остальных
    assert len(questions()) > 1 and client.predictions == [] and client.comments == []
    assert "кредиты исчерпаны" in caplog.text


def test_other_errors_do_not_stop_poll(fake_llm, fake_asknews):
    from forecast_bot import paths
    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    fake_llm.raise_exc = RuntimeError("ValueError: provider returned garbage")
    res = asyncio.run(R.run(client=FakeMetaculusClient(questions()), bot=ForecastBot(),
                            journal=Journal(paths.journal_db()), tournaments=["t"], submit=True))
    assert res.stopped_reason is None and len(res.rows) == len(questions())


def test_is_credit_error_markers():
    assert R.is_credit_error(OPENROUTER_402)
    assert R.is_credit_error("Insufficient credits. Add more using https://openrouter.ai/settings/credits")
    assert not R.is_credit_error("RateLimitError: 429 Too Many Requests")
    assert not R.is_credit_error("ValueError: could not parse")


def test_loop_line_shows_question_errors_and_credit_alarm():
    clock = Clock()

    async def run_once():
        res = R.RunResult(run_id="x", submit=True)
        res.rows.append({"status": "error"})
        res.stopped_reason = R.CREDITS_STOP
        return res

    stats = asyncio.run(R.poll_loop(run_once=run_once, duration_s=30 * 60, poll_s=10 * 60, run_budget=1.0,
                                    clock=clock, sleep=clock.sleep, day_spent=lambda: 0.0, day_cap=6.0))
    line = stats.line()
    assert stats.credit_stops == stats.polls == stats.question_errors > 0
    assert line.startswith("🔴 OpenRouter без кредитов") and f"ошибок по вопросам {stats.question_errors}" in line
    assert stats.stop_reasons == [R.CREDITS_STOP]                  # одна причина, а не по строке на опрос


def test_summary_flags_credit_errors():
    from forecast_bot import paths, summary
    from forecast_bot.journal import Journal

    Journal(paths.journal_db()).record(run_id="r", question_id=1, post_id=1, tournament="t", question_type="Binary",
                                       title="Q", url="u", mode="submit", model="m", status="error",
                                       error=OPENROUTER_402)
    out = summary.render(time.time() - 600)
    assert "🔴 **OpenRouter: кредиты исчерпаны (402)**" in out and "ошибок: 1" in out
