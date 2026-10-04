"""Сухой прогон Market Pulse на закрытых подвопросах 26Q3 «как будто на дату открытия». Ничего не отправляет.

    .venv/bin/python tools/pulse_dryrun.py [--per-group 2] [--budget 2.0]

- дата в промптах шаблона и агента подменена на день открытия подвопроса, quant — данные строго до него;
- поиск выключен (иначе в него попадут новости после периода), прогнозист Opus 5.5 high + подсказка quant;
- траты — приложение `forecast-lab`;
- фактический исход считается по ряду по тем же допущениям (quant.pulse_outcome) — тексты условий закрытых
  вопросов API не отдаёт, поэтому это проверка механики и порядка величин, а не официальный счёт.
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "forecast-lab"

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from datetime import datetime as _dt, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import guarded_llm, paths, quant as Q, run as R  # noqa: E402
from forecast_bot.run import load_env_file  # noqa: E402

GROUPS = (44534, 44536, 44531, 44527, 44530, 44528)  # VIX max, NVDA−MSFT, UST 10Y, Gold−ES, HY OAS, NQ−ES


class FixedClient:
    def __init__(self, qs):
        self.qs = qs

    def get_all_open_questions_from_tournament(self, _t):
        return list(self.qs)


def freeze_template_date(day):
    """Шаблон пишет в промпт «Today is datetime.now()» — подменяем на день открытия вопроса."""
    import metac_template_main as tm

    class Frozen(_dt):
        @classmethod
        def now(cls, tz=None):
            return cls(day.year, day.month, day.day, 12, 0, tzinfo=tz)

    tm.datetime = Frozen


def median_of(pred_json: str) -> float | None:
    data = json.loads(pred_json)
    pts = sorted((p["percentile"], p["value"]) for p in data.get("declared_percentiles", []))
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if p1 <= 0.5 <= p2:
            return v1 + (v2 - v1) * ((0.5 - p1) / (p2 - p1) if p2 > p1 else 0)
    return None


def interval(pred_json: str, lo=0.1, hi=0.9):
    pts = sorted((p["percentile"], p["value"]) for p in json.loads(pred_json).get("declared_percentiles", []))

    def at(q):
        for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
            if p1 <= q <= p2:
                return v1 + (v2 - v1) * ((q - p1) / (p2 - p1) if p2 > p1 else 0)
        return None
    return at(lo), at(hi)


async def main_async(per_group: int) -> list[dict]:
    from forecasting_tools import MetaculusClient

    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    client = MetaculusClient()
    journal = Journal(paths.journal_db())
    cache = paths.data_dir() / "polygon" / "series"
    out = []
    for pid in GROUPS:
        subs = await asyncio.to_thread(client.get_question_by_post_id, pid, "unpack_subquestions")
        subs = subs if isinstance(subs, list) else [subs]
        for q in sorted(subs, key=lambda x: x.open_time or _dt.max.replace(tzinfo=timezone.utc))[:per_group]:
            day = q.open_time.date()
            os.environ["FORECAST_ASOF"] = day.isoformat()
            freeze_template_date(day)
            res = await R.run(client=FixedClient([q]), bot=ForecastBot(), journal=journal, tournaments=["pulse-dry"],
                              submit=False, variant="pulse-dry")
            row = res.rows[0] if res.rows else {}
            title = (q.api_json or {}).get("title", "")
            hint = Q.pulse_quant(title, q.group_question_option or "", q.close_time.year, day, cache)
            actual = Q.pulse_outcome(title, q.group_question_option or "", q.close_time.year, cache)
            rec = {"post": pid, "label": q.group_question_option, "title": title[:70], "open": day.isoformat(),
                   "status": row.get("status"), "cost": row.get("cost_usd"), "actual": actual,
                   "quant_med": hint.percentiles[0.5] if hint else None,
                   "quant_10_90": (hint.percentiles[0.1], hint.percentiles[0.9]) if hint else None,
                   "range": (q.lower_bound, q.upper_bound) if hasattr(q, "lower_bound") else None}
            if row.get("status") == "ok":
                rec["llm_med"] = median_of(row["prediction"])
                rec["llm_10_90"] = interval(row["prediction"])
            else:
                rec["error"] = (row.get("error") or "")[:200]
            out.append(rec)
            print(json.dumps(rec, ensure_ascii=False, default=str))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-group", type=int, default=2)
    ap.add_argument("--budget", type=float, default=2.0)
    a = ap.parse_args()
    load_env_file(paths.env_path())
    os.environ.update({"FORECAST_PREDICTIONS": "1", "FORECAST_RESEARCH": "none", "FORECAST_QUANT_HINTS": "1",
                       "FORECAST_MODEL": "openrouter/anthropic/claude-opus-5.5", "FORECAST_REASONING": "high"})
    import forecast_bot.bot  # noqa: F401  — грузит шаблон как metac_template_main

    guarded_llm.start_run(a.budget)
    recs = asyncio.run(main_async(a.per_group))
    (paths.data_dir() / "polygon" / "pulse_dryrun.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in recs) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
