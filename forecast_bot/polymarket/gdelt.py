"""Бесплатный поиск новостей GDELT DOC 2.0 с отсечкой по дате — для бэктеста «как будто на дату t».

⚠ `enddatetime` у GDELT НЕСТРОГИЙ: запрос с enddatetime=2026-08-15 00:00 вернул статьи с seendate
2026-08-15 20:00 … 2026-08-16 00:00, с enddatetime=2026-06-01 00:00 — все 50 статей из 06-01 18:15 … 06-02 00:00
(живые запросы 05.10.2026), то есть сервер добирает до +24 ч. При sort=datedesc верх выдачи целиком из этих
«лишних» суток, и строгий фильтр оставлял бы пусто. Поэтому: (1) серверу отдаём конец окна на SERVER_LEAK раньше t;
(2) отсечка всё равно здесь, по seendate — только статьи, увиденные GDELT строго ДО t. seendate ≥ даты
публикации, так что отсечка безопасна.
Темп — не чаще раза в 6 с (требование API), см. http.MIN_INTERVAL_S.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from forecast_bot.polymarket.http import get_json

URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SERVER_LEAK = timedelta(hours=24)  # насколько сервер «перебирает» за enddatetime (см. шапку)
STOP = set("will the a an of in on at to by be is are for and or with than before after from this that which who "
           "what when does do did has have not yes no market vs any more less above below between end close 2024 2025 "
           "2026 2027 january february march april may june july august september october november december".split())


@dataclass
class Article:
    seen: datetime
    title: str
    url: str
    domain: str
    language: str


def keywords(question: str, k: int = 5) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9.'&-]{2,}", question)
    out: list[str] = []
    for w in words:
        lw = w.lower().strip(".'")
        if len(lw) < 3 or lw in STOP or lw in (x.lower() for x in out):  # GDELT отвергает слова короче 3 букв текстом
            continue
        out.append(w.strip(".'"))
        if len(out) >= k:
            break
    return out


def _parse_seen(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def strictly_before(arts: list[Article], cutoff: datetime) -> list[Article]:
    return [a for a in arts if a.seen < cutoff]


def search(question: str, cutoff: datetime, days: int = 14, max_records: int = 50, retries: int = 4) -> list[Article]:
    words = keywords(question)
    if not words:
        return []
    start = cutoff - timedelta(days=days)
    d = get_json(URL, {"query": " ".join(words), "mode": "artlist", "format": "json", "sort": "datedesc",
                       "maxrecords": max_records, "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                       "enddatetime": (cutoff - SERVER_LEAK).strftime("%Y%m%d%H%M%S")}, retries=retries)
    arts = []
    for a in (d or {}).get("articles", []) or []:
        seen = _parse_seen(a.get("seendate", ""))
        if seen:
            arts.append(Article(seen, (a.get("title") or "").strip(), a.get("url") or "", a.get("domain") or "",
                                a.get("language") or ""))
    return strictly_before(arts, cutoff)


def as_research(arts: list[Article], limit: int = 12) -> str:
    """Заголовки как источники [S#] — без вызова модели; числа в них — текст источника."""
    if not arts:
        return "No news articles were found before the forecast date."
    lines = [f"Recent news headlines seen before the forecast date (GDELT), newest first:"]
    for i, a in enumerate(arts[:limit], 1):
        lines.append(f"[S{i}] {a.seen:%Y-%m-%d %H:%M} UTC · {a.domain} · {a.title} ({a.url})")
    return "\n".join(lines)
