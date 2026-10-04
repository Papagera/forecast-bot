"""Журнал прогнозов (SQLite): что спрогнозировали, чем, за сколько, и чем кончилось.

Нужен для двух вещей: «не прогнозировать дважды» (вторая линия после флага Metaculus
`already_forecasted`) и калибровки на этапе 2 (итог после резолва).
Dry-строки НЕ блокируют отправку — блокирует только `mode='submit' AND status='ok'`.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    question_id   INTEGER NOT NULL,
    post_id       INTEGER,
    tournament    TEXT,
    question_type TEXT,
    title         TEXT,
    url           TEXT,
    mode          TEXT NOT NULL CHECK (mode IN ('dry', 'submit')),
    status        TEXT NOT NULL,
    model         TEXT,
    prediction    TEXT,
    reasoning     TEXT,
    cost_usd      REAL,
    llm_calls     INTEGER,
    asknews_calls INTEGER DEFAULT 0,
    error         TEXT,
    created_at    REAL NOT NULL,
    submitted_at  REAL,
    resolution    TEXT,
    resolved_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_forecasts_q ON forecasts(question_id);
CREATE INDEX IF NOT EXISTS idx_forecasts_created ON forecasts(created_at);
"""

EXTRA_COLUMNS = (
    ("research_numbers", "INTEGER"),      # чисел в справке исследования
    ("research_unverified", "INTEGER"),   # из них не нашлось в процитированных источниках (факт отброшен)
    ("research_dropped", "TEXT"),         # отброшенные строки — для ручного разбора
    ("forecast_numbers", "INTEGER"),      # чисел в рассуждении прогнозиста
    ("forecast_unverified", "INTEGER"),   # из них нет ни в исследовании, ни в вопросе
    ("variant", "TEXT"),                  # метка варианта замера (блок 2.1)
)

# Статусы строки.
OK = "ok"
ERROR = "error"
SKIPPED_BUDGET = "skipped_budget"
SKIPPED_ASKNEWS = "skipped_asknews_quota"


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Колонки блока 2.1 (сверка чисел). CREATE TABLE IF NOT EXISTS их в старую базу не добавит.
            for col, typ in EXTRA_COLUMNS:
                try:
                    c.execute(f"ALTER TABLE forecasts ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def already_submitted(self, question_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM forecasts WHERE question_id = ? AND mode = 'submit' AND status = ? LIMIT 1",
                (question_id, OK),
            ).fetchone()
        return row is not None

    def last_submitted_at(self, question_id: int) -> Optional[float]:
        """Время последней успешной отправки по вопросу (spot-турниры обновляют прогноз по расписанию)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(submitted_at) t FROM forecasts WHERE question_id = ? AND mode = 'submit' AND status = ?",
                (question_id, OK),
            ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def asknews_calls_this_month(self, now: Optional[float] = None) -> int:
        d = dt.datetime.fromtimestamp(now or time.time())
        since = dt.datetime(d.year, d.month, 1).timestamp()
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(asknews_calls), 0) n FROM forecasts WHERE created_at >= ?", (since,)
            ).fetchone()
        return int(row["n"])

    def record(self, **fields: Any) -> int:
        fields.setdefault("created_at", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as c:
            cur = c.execute(f"INSERT INTO forecasts ({cols}) VALUES ({marks})", tuple(fields.values()))
            return int(cur.lastrowid)

    def rows(self, run_id: Optional[str] = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if run_id:
                return list(c.execute("SELECT * FROM forecasts WHERE run_id = ? ORDER BY id", (run_id,)))
            return list(c.execute("SELECT * FROM forecasts ORDER BY id"))

    def set_resolution(self, question_id: int, resolution: str, resolved_at: Optional[float] = None) -> int:
        """Для этапа 2 (калибровка): проставить итог всем строкам вопроса."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE forecasts SET resolution = ?, resolved_at = ? WHERE question_id = ?",
                (resolution, resolved_at or time.time(), question_id),
            )
            return cur.rowcount
