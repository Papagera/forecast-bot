"""Живой смоук MC: один вопрос, Opus 5.5 high без поиска, dry, траты в forecast-lab.

    .venv/bin/python tools/mc_smoke.py <post_id>
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "forecast-lab"

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot import guarded_llm, paths, run as R  # noqa: E402


class FixedClient:
    def __init__(self, qs):
        self.qs = qs

    def get_all_open_questions_from_tournament(self, _t):
        return list(self.qs)


def main() -> int:
    R.load_env_file(paths.env_path())
    os.environ.update({"FORECAST_PREDICTIONS": "1", "FORECAST_RESEARCH": "none",
                       "FORECAST_MODEL": "openrouter/anthropic/claude-opus-5.5", "FORECAST_REASONING": "high"})
    from forecasting_tools import MetaculusClient

    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    q = MetaculusClient().get_question_by_post_id(int(sys.argv[1]))
    guarded_llm.start_run(0.5)
    res = asyncio.run(R.run(client=FixedClient([q]), bot=ForecastBot(), journal=Journal(paths.journal_db()),
                            tournaments=["mc-smoke"], submit=False, variant="mc-smoke"))
    row = res.rows[0]
    tail = [l for l in (row.get("reasoning") or "").splitlines() if l.strip()][-6:]
    print(json.dumps({"status": row["status"], "cost": row.get("cost_usd"), "options": q.options,
                      "prediction": json.loads(row["prediction"]) if row.get("prediction") else None,
                      "tail": tail}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
