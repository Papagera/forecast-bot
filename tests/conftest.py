"""Офлайн-окружение: фейковые ключи, леджер/журнал во tmp, сеть закрыта, LLM и AskNews — фейки."""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_REAL_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "EXA_API_KEY",
              "ASKNEWS_CLIENT_ID", "ASKNEWS_SECRET", "FORECAST_SUBMIT", "AI_KILL",
              "FORECAST_MODEL", "FORECAST_PARSER_MODEL", "FORECAST_PREDICTIONS")


@pytest.fixture(autouse=True)
def offline_env(tmp_path, monkeypatch):
    for k in _REAL_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("METACULUS_TOKEN", "fake-metaculus")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter")
    monkeypatch.setenv("ASKNEWS_API_KEY", "fake-asknews")
    monkeypatch.setenv("AI_LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("FORECAST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FORECAST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FORECAST_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("FORECAST_ENV_FILE", str(tmp_path / "absent.env"))

    def no_network(self, address, *a, **k):
        if self.family == socket.AF_UNIX:
            return _connect(self, address, *a, **k)
        raise RuntimeError(f"сеть в тестах запрещена: {address}")

    _connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", no_network)

    from forecast_bot import ai_guard, guarded_llm

    monkeypatch.setattr(guarded_llm, "RETRY_BACKOFF_S", [0.0])
    monkeypatch.setitem(ai_guard.APP_LIMITS, "forecast", {"day_usd": 3.0})
    monkeypatch.setitem(ai_guard.LIMITS, "per_user_day_calls", 20)
    # Фейк отвечает мгновенно: 4 вопроса × 16 вызовов за секунду упёрлись бы в RPM 30 машины.
    # Поведение при RPM проверяет отдельный тест (test_guard.py::test_rpm_waits_for_window).
    monkeypatch.setitem(ai_guard.LIMITS, "rpm", 10_000)
    monkeypatch.setitem(ai_guard.LIMITS, "tpm", 10_000_000)
    guarded_llm.reset_counters()
    yield
    guarded_llm.reset_counters()


@pytest.fixture
def fake_llm(monkeypatch):
    from fakes import FakeLlm

    from forecast_bot import guarded_llm

    guarded_llm.install_sentinel()
    llm = FakeLlm()
    monkeypatch.setitem(guarded_llm._BACKEND, "acompletion", llm)
    return llm


@pytest.fixture
def fake_asknews(monkeypatch):
    from forecasting_tools import AskNewsSearcher

    calls: list[str] = []

    async def fake(self, preset, prompt):
        calls.append(preset)
        return "Новости: ничего нового."

    monkeypatch.setattr(AskNewsSearcher, "call_preconfigured_version", fake)
    return calls
