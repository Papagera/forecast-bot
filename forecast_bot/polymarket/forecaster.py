"""Прогноз рынка Polymarket ботом «как будто на дату t»: дата в промптах заморожена, исследование — готовый текст.

Модель — прогнозист шаблона (Opus 5.5 high по умолчанию, FORECAST_MODEL), все вызовы через ai_guard
(приложение `polymarket`). Цену рынка бот НЕ получает — нужна независимая оценка.
"""
from __future__ import annotations

import os
import zlib
from datetime import datetime
from typing import Any, Optional

from forecast_bot.polymarket.markets import Market


def _freeze_template_date(day: datetime) -> None:
    import metac_template_main as tm

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(day.year, day.month, day.day, day.hour, day.minute, tzinfo=tz)

    tm.datetime = Frozen


def make_question(m: Market, point: str, t: datetime) -> Any:
    from forecasting_tools import BinaryQuestion

    return BinaryQuestion(
        question_text=m.question,
        id_of_question=zlib.crc32(f"{m.id}:{point}".encode()) % 10**9,
        id_of_post=zlib.crc32(m.id.encode()) % 10**9,
        page_url=m.url,
        background_info=m.description,
        resolution_criteria=m.description,
        fine_print="",
        close_time=m.closed,
    )


async def forecast(m: Market, point: str, t: datetime, research: str) -> tuple[Optional[float], str]:
    """(p_YES или None, ошибка). Ledger user — `polymarket:pm<id>-<point>-<режим>` (ставит вызывающий)."""
    from forecast_bot.bot import ForecastBot

    class PolyBot(ForecastBot):
        async def run_research(self, question: Any) -> str:  # исследование уже собрано (или пусто)
            return research

    os.environ["FORECAST_PREDICTIONS"] = os.environ.get("FORECAST_PREDICTIONS", "1")
    _freeze_template_date(t)
    bot = PolyBot()
    report = await bot.forecast_question(make_question(m, point, t), return_exceptions=True)
    if isinstance(report, BaseException):
        return None, f"{type(report).__name__}: {str(report)[:300]}"
    return float(report.prediction), ""
