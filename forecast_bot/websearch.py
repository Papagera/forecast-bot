"""Веб-поиск исследователя через OpenRouter (plugins web, Exa) — замена AskNews в бою (решение Никиты 05.10.2026).

Один инструментальный вызов = ровно один поиск Exa: дешёвая модель + плагин `web`, текст ответа модели не нужен —
берём аннотации `url_citation` (url, заголовок, выдержка). Дальше агент получает их как источник [S#] — правило
сверки чисел то же (forecast_bot/verify.py): число справки должно буквально встречаться в выдержке.

Цена (openrouter.ai/docs/guides/features/plugins/web-search, 05.10.2026): Exa — $0.007 за запрос до 10 результатов.
Счёт OpenRouter (`usage.cost`) уже включает поиск; в леджер он идёт отдельной строкой (guarded_llm.split_search_cost).
⚠ Поиск НЕ режет по дате — для бэктестов (утечка будущего) не использовать: там только GDELT с отсечкой.
"""
from __future__ import annotations

import os
from typing import Any

from forecast_bot import guarded_llm

EXA_PRICE = 0.007
MAX_RESULTS = 8          # ≤10 — в цене одного запроса
SNIPPET_LIMIT = 500


def search_model() -> str:
    return os.environ.get("FORECAST_SEARCH_MODEL", "openrouter/google/gemini-3.8-flash")


def annotations(resp: Any) -> list[dict]:
    msg = resp.choices[0].message
    d = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
    anns = d.get("annotations") or (d.get("provider_specific_fields") or {}).get("annotations") or []
    out = []
    for a in anns:
        c = a.get("url_citation") or a
        if c.get("url"):
            out.append({"url": c["url"], "title": (c.get("title") or "").strip(), "content": (c.get("content") or "").strip()})
    return out


def format_results(results: list[dict]) -> str:
    if not results:
        return "Web search returned no results."
    lines = []
    for i, r in enumerate(results, 1):
        snippet = " ".join(r["content"].split())[:SNIPPET_LIMIT]
        lines.append(f"{i}. {r['title']} — {r['url']}\n   {snippet}")
    return "\n".join(lines)


async def search(query: str) -> str:
    resp = await guarded_llm.guarded_completion(
        search_model(),
        [{"role": "user", "content": f"Search the web for: {query}\nReply with the single word: ok"}],
        max_tokens=60, temperature=0, search_price=EXA_PRICE,
        extra_body={"plugins": [{"id": "web", "engine": "exa", "max_results": MAX_RESULTS}]},
    )
    return format_results(annotations(resp))
