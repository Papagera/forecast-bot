"""Разведка: где litellm отдаёт результаты веб-поиска OpenRouter (plugins web, Exa). Вызов через ai_guard (forecast-lab)."""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "forecast-lab"

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot import guarded_llm, paths  # noqa: E402
from forecast_bot.run import load_env_file  # noqa: E402


async def main() -> None:
    load_env_file(paths.env_path())
    guarded_llm.install_sentinel()
    guarded_llm.start_run(0.2)
    resp = await guarded_llm.guarded_completion(
        "openrouter/google/gemini-3.8-flash",
        [{"role": "user", "content": "Search the web for: Federal Reserve October 2026 rate decision expectations. Reply only 'ok'."}],
        max_tokens=50, extra_body={"plugins": [{"id": "web", "engine": "exa", "max_results": 8}]})
    msg = resp.choices[0].message
    print("content:", repr(msg.content)[:120])
    print("message keys:", sorted(k for k in (msg.model_dump() if hasattr(msg, "model_dump") else {}).keys()))
    d = msg.model_dump() if hasattr(msg, "model_dump") else {}
    ann = d.get("annotations") or (d.get("provider_specific_fields") or {}).get("annotations")
    print("annotations:", len(ann or []))
    for a in (ann or [])[:3]:
        print(json.dumps(a, ensure_ascii=False)[:400])
    print("usage:", resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else resp.usage)
    print("hidden cost:", (getattr(resp, "_hidden_params", {}) or {}).get("additional_headers", {}).get("llm_provider-x-litellm-response-cost"))


if __name__ == "__main__":
    asyncio.run(main())
