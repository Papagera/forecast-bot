"""Сырьё для калибровки (этап 4, 06.10.2026): все прогнозы вопроса до агрегации, их разброс и «сила справки».

Без новых вызовов ИИ: бот и так делает FORECAST_PREDICTIONS=5 прогнозов, в журнал раньше уходила только медиана.
- binary — список вероятностей; разброс = max − min, sd;
- multiple choice — по каждому варианту max − min, берётся наибольший;
- numeric/discrete — разброс медиан (50-й перцентиль каждого прогноза).
«Сила справки»: число фактов справки с подтверждённой ссылкой [S#] и был ли среди процитированных официальный
источник (данные FRED или страница на домене госоргана / международной организации).
"""
from __future__ import annotations

import json
import re
import statistics
from typing import Any, Optional
from urllib.parse import urlparse

OFFICIAL_HOST = re.compile(r"(^|\.)(gov|mil|int)(\.[a-z]{2})?$|(^|\.)(europa\.eu|un\.org|who\.int|imf\.org|"
                           r"worldbank\.org|oecd\.org|bis\.org|federalreserve\.gov|ecb\.europa\.eu|"
                           r"metaculus\.com)$", re.I)


def _value(p: Any) -> Any:
    """Представление одного прогноза: число, {вариант: вероятность} или медиана распределения."""
    if isinstance(p, (int, float)):
        return float(p)
    opts = getattr(p, "predicted_options", None)
    if opts is not None:
        return {o.option_name: float(o.probability) for o in opts}
    pcts = getattr(p, "declared_percentiles", None)
    if pcts:
        pts = sorted((float(x.percentile), float(x.value)) for x in pcts)
        for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
            if p0 <= 0.5 <= p1:
                return v0 if p1 == p0 else v0 + (v1 - v0) * (0.5 - p0) / (p1 - p0)
        return pts[len(pts) // 2][1]
    return None


def summarize(predictions: list) -> dict:
    """Колонки журнала: predictions_all (JSON), predictions_n, predictions_spread, predictions_sd."""
    vals = [v for v in (_value(getattr(p, "prediction_value", p)) for p in predictions) if v is not None]
    out: dict[str, Any] = {"predictions_all": json.dumps(vals, ensure_ascii=False), "predictions_n": len(vals),
                           "predictions_spread": None, "predictions_sd": None}
    if not vals:
        return out
    if isinstance(vals[0], dict):
        keys = set().union(*vals)
        ranges = [max(v.get(k, 0.0) for v in vals) - min(v.get(k, 0.0) for v in vals) for k in keys]
        out["predictions_spread"] = max(ranges) if ranges else None
        return out
    nums = [float(v) for v in vals]
    out["predictions_spread"] = max(nums) - min(nums)
    out["predictions_sd"] = statistics.pstdev(nums) if len(nums) > 1 else 0.0
    return out


def is_official(meta: str) -> bool:
    """`meta` — «<инструмент> <url|series_id|…>» из агента."""
    name, _, detail = (meta or "").partition(" ")
    if name == "fred_series":
        return True
    if name == "fetch_url":
        host = (urlparse(detail.strip()).hostname or "").lower()
        return bool(host and OFFICIAL_HOST.search(host))
    return False


def research_strength(facts_cited: int, cited_sources: set, source_meta: dict) -> dict:
    return {"research_cited_facts": int(facts_cited),
            "research_official": int(any(is_official(source_meta.get(s, "")) for s in cited_sources))}


def spread_of(row: dict) -> Optional[float]:
    return row.get("predictions_spread")
