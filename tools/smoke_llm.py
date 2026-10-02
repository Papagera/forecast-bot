"""Живой смоук пути «модель → ai_guard → леджер»: один короткий вызов, печать строки леджера.

    .venv/bin/python tools/smoke_llm.py [модель]

Ключ — из .env основного чекаута; значение не печатается. Стоимость — центы и меньше.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import ai_guard, guarded_llm, paths  # noqa: E402
from forecast_bot.run import load_env_file  # noqa: E402


def main() -> int:
    load_env_file(paths.env_path())
    model = sys.argv[1] if len(sys.argv) > 1 else "openrouter/openai/gpt-4o-mini"
    guarded_llm.install_sentinel()
    llm = guarded_llm.GuardedLlm(model, max_tokens=20, temperature=0, allowed_tries=1)
    t0 = time.time()
    token = guarded_llm.CURRENT_USER.set("smoke")
    try:
        answer = asyncio.run(llm.invoke("Reply with exactly one word: OK"))
    finally:
        guarded_llm.CURRENT_USER.reset(token)
    conn = ai_guard._conn()
    try:
        row = conn.execute('SELECT provider, model, "user", tokens_in, tokens_out, cost_usd FROM usage '
                           'WHERE "user" = ? AND ts >= ? ORDER BY ts DESC LIMIT 1', ("forecast:smoke", t0)).fetchone()
    finally:
        conn.close()
    print(f"ответ: {answer!r}")
    print(f"леджер {ai_guard._ledger_path()}: {dict(row) if row else 'СТРОКИ НЕТ'}")
    return 0 if row else 1


if __name__ == "__main__":
    sys.exit(main())
