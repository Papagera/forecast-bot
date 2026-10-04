"""Замер вариантов (блок 2.1) на одном наборе открытых вопросов — только dry, ничего не отправляется.

    .venv/bin/python tools/compare_variants.py --n 16 [--variants A,B,C,D] [--budget 7.5]

A — агент Opus 5.5 high на всех шагах; B — исследование Haiku 4.5, итог Opus 5.5 high;
C — исследование Haiku 4.5, итог Gemini 3.5 Flash; D — шаблон: Gemini 3.5 Flash + AskNews свежие.
Траты идут в отдельное приложение леджера `forecast-lab` (свой суточный потолок), боевой «forecast» не трогается.
Набор вопросов фиксируется в data/polygon/variants_questions.json и переиспользуется.
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "forecast-lab"  # ДО импорта forecast_bot: имя приложения читается при импорте

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import guarded_llm, paths, run as R  # noqa: E402
from forecast_bot.run import load_env_file  # noqa: E402

OPUS = "openrouter/anthropic/claude-opus-5.5"
HAIKU = "openrouter/anthropic/claude-haiku-4.5"
FLASH = "openrouter/google/gemini-3.5-flash"
SONNET = "openrouter/anthropic/claude-sonnet-5.5"
FLASH38 = "openrouter/google/gemini-3.8-flash"  # актуальная Flash на OpenRouter, 04.10.2026
VARIANTS = {
    "A": {"FORECAST_RESEARCH": "agent", "FORECAST_AGENT_MODEL": OPUS, "FORECAST_MODEL": OPUS, "FORECAST_REASONING": "high"},
    "B": {"FORECAST_RESEARCH": "agent", "FORECAST_AGENT_MODEL": HAIKU, "FORECAST_MODEL": OPUS, "FORECAST_REASONING": "high"},
    "C": {"FORECAST_RESEARCH": "agent", "FORECAST_AGENT_MODEL": HAIKU, "FORECAST_MODEL": FLASH, "FORECAST_REASONING": ""},
    "D": {"FORECAST_RESEARCH": "asknews-latest", "FORECAST_AGENT_MODEL": "", "FORECAST_MODEL": FLASH, "FORECAST_REASONING": ""},
    # Замена Haiku 4.5 (retirement не раньше 15.10.2026): исследователь меняется, итог — Opus 5.5 high как в B.
    "H": {"FORECAST_RESEARCH": "agent", "FORECAST_AGENT_MODEL": HAIKU, "FORECAST_MODEL": OPUS, "FORECAST_REASONING": "high"},
    "S": {"FORECAST_RESEARCH": "agent", "FORECAST_AGENT_MODEL": SONNET, "FORECAST_MODEL": OPUS, "FORECAST_REASONING": "high"},
    "G": {"FORECAST_RESEARCH": "agent", "FORECAST_AGENT_MODEL": FLASH38, "FORECAST_MODEL": OPUS, "FORECAST_REASONING": "high"},
}
SOURCES = ("bot-testing-area", 33108)  # песочница + Metaculus Cup Fall 2026 (открытые вопросы)
QUOTA = {"BinaryQuestion": 7, "NumericQuestion": 4, "DiscreteQuestion": 1, "MultipleChoiceQuestion": 4}


class FixedClient:
    """Отдаёт один и тот же набор вопросов любому варианту; отправка невозможна (dry)."""

    def __init__(self, questions: list) -> None:
        self.questions = questions

    def get_all_open_questions_from_tournament(self, _t):
        return list(self.questions)


def select_questions(n: int) -> list:
    from forecasting_tools import MetaculusClient

    store = paths.data_dir() / "polygon" / "variants_questions.json"
    client = MetaculusClient()
    if store.exists():
        ids = json.loads(store.read_text())
        out = []
        for pid in ids:  # групповые посты разворачиваются в подвопросы; закрытые с тех пор — отбрасываются
            got = client.get_question_by_post_id(pid, "unpack_subquestions")
            for q in (got if isinstance(got, list) else [got]):
                if str(getattr(q, "state", "")).endswith("OPEN") and q not in out:
                    out.append(q)
        return out
    pool = [q for t in SOURCES for q in client.get_all_open_questions_from_tournament(t)]
    picked, left = [], dict(QUOTA)
    for q in pool:
        k = type(q).__name__
        if left.get(k, 0) > 0 and len(picked) < n:
            picked.append(q)
            left[k] -= 1
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps([q.id_of_post for q in picked]))
    return picked


async def run_variant(name: str, questions: list) -> R.RunResult:
    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    for k, v in VARIANTS[name].items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return await R.run(client=FixedClient(questions), bot=ForecastBot(), journal=Journal(paths.journal_db()),
                       tournaments=["variants"], submit=False, variant=name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--variants", default="A,B,C,D")
    ap.add_argument("--budget", type=float, default=7.5, help="лимит $ на весь замер")
    args = ap.parse_args()
    load_env_file(paths.env_path())
    os.environ["FORECAST_PREDICTIONS"] = "1"
    os.environ.setdefault("FORECAST_AGENT_MAX_NEWS", "2")
    questions = select_questions(args.n)
    print(f"вопросов: {len(questions)} — " + ", ".join(sorted({type(q).__name__ for q in questions})))
    guarded_llm.start_run(args.budget)
    # Стенд гоняет несколько вариантов по ОДНИМ вопросам в один день: счётчик гарда «вызовов на вопрос в сутки»
    # (40 в бою) иначе кончается на третьем варианте. Денежные потолки (forecast-lab $8/сутки, --budget) не трогаем.
    from forecast_bot import ai_guard
    ai_guard.LIMITS["per_user_day_calls"] = 200
    for name in args.variants.split(","):
        res = asyncio.run(run_variant(name, questions))
        print(f"вариант {name}: run {res.run_id} ok={res.count('ok')} error={res.count('error')} "
              f"skip={len(res.rows) - res.count('ok') - res.count('error')}"
              + (f" стоп: {res.stopped_reason}" if res.stopped_reason else ""))
        if res.stopped_reason:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
