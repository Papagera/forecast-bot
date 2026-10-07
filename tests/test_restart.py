"""Перезапуск цикла: повторы на сбоях GitHub (07.10.2026: HTTP 500 оборвал цепочку на час)."""
from __future__ import annotations

import subprocess

from forecast_bot import restart as RS

E500 = "could not create workflow dispatch event: HTTP 500 (https://api.github.com/repos/x/y/actions/workflows/1/dispatches)"


def _runner(results):
    calls = []

    def run(repo, ref):
        calls.append((repo, ref))
        r = results[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    return run, calls


def test_retries_5xx_with_pauses_then_succeeds():
    run, calls = _runner([(1, E500), (1, E500), (0, "")])
    slept = []
    assert RS.restart("o/r", "main", run=run, sleep=slept.append, log=lambda *_: None) == 0
    assert len(calls) == 3 and slept == [30, 60] and calls[0] == ("o/r", "main")


def test_gives_up_after_all_pauses_and_fails_step():
    run, calls = _runner([(1, E500)] * 10)
    slept = []
    assert RS.restart("o/r", "main", run=run, sleep=slept.append, log=lambda *_: None) == 1
    assert len(calls) == 6 and slept == [30, 60, 120, 240, 300] and sum(slept) == RS.MAX_WAIT_S


def test_4xx_is_not_retried():
    run, calls = _runner([(1, "HTTP 403: Resource not accessible by integration"), (0, "")])
    slept = []
    assert RS.restart("o/r", "main", run=run, sleep=slept.append, log=lambda *_: None) == 1
    assert len(calls) == 1 and slept == []


def test_network_errors_and_timeouts_are_retried():
    run, calls = _runner([subprocess.TimeoutExpired("gh", 120), (1, "dial tcp: connection reset by peer"), (0, "")])
    slept = []
    assert RS.restart("o/r", "main", run=run, sleep=slept.append, log=lambda *_: None) == 0 and slept == [30, 60]
    assert RS.retryable("HTTP 502 Bad Gateway") and RS.retryable("TLS handshake timeout")
    assert not RS.retryable("HTTP 422 Unprocessable") and not RS.retryable("unknown flag --foo")


def test_rate_limit_429_is_retried():
    run, calls = _runner([(1, "HTTP 429: secondary rate limit"), (0, "")])
    slept = []
    assert RS.restart("o/r", "main", run=run, sleep=slept.append, log=lambda *_: None) == 0 and slept == [30]
