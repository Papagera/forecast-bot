"""Где лежат .env, журнал и отчёты — всегда в ОСНОВНОМ чекауте, не в рабочем дереве.

Рабочие деревья (`<проект>/.claude/worktrees/<задача>/`) расходные: их снимают после задачи, и
относительный `data/` уехал бы вместе с ними (так clipper потерял scout.db 29.09.2026).
Корень основного чекаута = корень пакета с отрезанным хвостом `/.claude/worktrees/<имя>`.
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def main_checkout(root: Path = PACKAGE_ROOT) -> Path:
    parts = root.parts
    for i in range(len(parts) - 2):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return Path(*parts[:i])
    return root


def _env_dir(name: str, default: Path) -> Path:
    env = os.environ.get(name, "").strip()
    return Path(env).expanduser() if env else default


def env_path() -> Path:
    return _env_dir("FORECAST_ENV_FILE", main_checkout() / ".env")


def data_dir() -> Path:
    return _env_dir("FORECAST_DATA_DIR", main_checkout() / "data")


def journal_db() -> Path:
    return data_dir() / "journal.db"


def reports_dir() -> Path:
    return _env_dir("FORECAST_REPORTS_DIR", main_checkout() / "_отчёты")


def state_dir() -> Path:
    """Лок и лог LaunchAgent — вне репозитория."""
    return _env_dir("FORECAST_STATE_DIR", Path.home() / ".forecast-bot")
