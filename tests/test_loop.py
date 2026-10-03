"""Цикл опроса (блок 2.2) на фейковых часах: расписание опросов, лимиты внутри цикла, устойчивость к ошибкам."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fakes import FakeMetaculusClient, questions

from forecast_bot import guarded_llm, run as R


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def _loop(run_once, clock, *, minutes=60, poll=10, budget=1.0, day_spent=lambda: 0.0, cap=6.0):
    return asyncio.run(R.poll_loop(run_once=run_once, duration_s=minutes * 60, poll_s=poll * 60, run_budget=budget,
                                   clock=clock, sleep=clock.sleep, day_spent=day_spent, day_cap=cap))


def _empty():
    return R.RunResult(run_id="x", submit=True)


def test_polls_every_interval_until_window_ends():
    clock, seen = Clock(), []

    async def once():
        seen.append(clock.t)
        return _empty()

    stats = _loop(once, clock)
    assert stats.polls == 6 and [t - seen[0] for t in seen] == [0, 600, 1200, 1800, 2400, 3000]
    assert stats.finished_at - stats.started_at < 3600  # не выходит за окно


def test_run_budget_reset_on_every_poll():
    clock, starts = Clock(), []

    async def once():
        starts.append((guarded_llm.RUN_STARTED_AT, guarded_llm.RUN_BUDGET_USD))
        clock.t += 5
        return _empty()

    import forecast_bot.guarded_llm as g
    real = g.start_run
    g.start_run = lambda b: (setattr(g, "RUN_BUDGET_USD", b), setattr(g, "RUN_STARTED_AT", clock.t))
    try:
        _loop(once, clock, minutes=30)
    finally:
        g.start_run = real
    assert len({s for s, _ in starts}) == len(starts) == 3 and all(b == 1.0 for _, b in starts)


def test_day_cap_skips_polls_without_touching_metaculus():
    clock, calls = Clock(), []

    async def once():
        calls.append(1)
        return _empty()

    stats = _loop(once, clock, day_spent=lambda: 6.0)
    assert calls == [] and stats.polls == 0 and stats.skipped_day_cap == 6


def test_day_cap_reached_mid_loop_stops_further_polls():
    clock, spent = Clock(), {"v": 0.0}

    async def once():
        spent["v"] += 2.5
        return _empty()

    stats = _loop(once, clock, day_spent=lambda: spent["v"])
    assert stats.polls == 3 and stats.skipped_day_cap == 3  # 0 → 2.5 → 5.0 → 7.5 ≥ 6


def test_exception_in_poll_does_not_stop_loop():
    clock, n = Clock(), {"i": 0}

    async def once():
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("Metaculus 502")
        return _empty()

    stats = _loop(once, clock)
    assert stats.polls == 6 and stats.errors == 1


def test_last_found_time_and_forecasts_counted():
    clock, n = Clock(), {"i": 0}

    async def once():
        n["i"] += 1
        res = _empty()
        if n["i"] == 3:
            res.rows = [{"status": "ok"}, {"status": "ok"}]
            found["t"] = clock.t
        return res

    found = {}
    stats = _loop(once, clock)
    assert stats.forecasts == 2 and stats.last_found_at == found["t"]
    assert "последний найденный вопрос" in stats.line() and "опросов 6" in stats.line()


def test_stop_at_does_not_start_new_questions(fake_llm, fake_asknews):
    from forecast_bot import paths
    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    res = asyncio.run(R.run(client=FakeMetaculusClient(questions()), bot=ForecastBot(),
                            journal=Journal(paths.journal_db()), tournaments=["t"], submit=True,
                            stop_at=time.time() - 1))
    assert res.rows == [] and fake_llm.calls == [] and res.stopped_reason == "время цикла вышло"


def test_main_loop_mode_saves_stats(monkeypatch, tmp_path):
    async def fake_run(**k):
        return R.RunResult(run_id="t", submit=False)

    async def fake_loop(**k):
        assert k["duration_s"] == 1.5 * 60 and k["poll_s"] == 0.5 * 60 and k["run_budget"] == 1.0
        return R.LoopStats(started_at=1.0, finished_at=2.0, polls=3)

    monkeypatch.setattr(R, "run", fake_run)
    monkeypatch.setattr(R, "poll_loop", fake_loop)
    assert R.main(["--loop-minutes", "1.5", "--poll-minutes", "0.5", "--run-budget", "1.0"]) == 0
    from forecast_bot import paths

    data = json.loads((paths.state_dir() / "loop.json").read_text())
    assert data["polls"] == 3


def test_summary_shows_loop_line():
    from forecast_bot import paths, summary

    R.LoopStats(started_at=time.time() - 100, finished_at=time.time(), polls=7, last_found_at=time.time() - 50) \
        .save(paths.state_dir() / "loop.json")
    out = summary.render(time.time() - 600)
    assert "Цикл: опросов 7" in out and "последний найденный вопрос 20" in out
