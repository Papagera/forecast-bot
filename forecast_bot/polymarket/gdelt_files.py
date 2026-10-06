"""Заголовки новостей из статических выгрузок GDELT 2.0 GKG (data.gdeltproject.org) — для бэктеста «как будто на t».

API GDELT DOC держал 429 часами (05.10.2026), а для объёма GDELT сам рекомендует выгрузки (решение income 05.10.2026).
Файл `YYYYMMDDHHMMSS.gkg.csv.zip` — пачка статей, которые GDELT увидел за 15 минут ДО метки времени файла
(колонка 1 = та же метка, 27 колонок, заголовок в колонке 26 `<PAGE_TITLE>`; проверено на файле 20260815120000).

Отсечка двойная и строгая: (1) берём только файлы с меткой ≤ t − 1 ч (`file_times`); (2) каждую строку — только с
меткой < t (`parse_gkg` + `strictly_before`). Окно — `days` суток до t, выборка `per_hour` файлов в час (не весь архив).
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from forecast_bot.polymarket.gdelt import Article, keywords

BASE = "https://data.gdeltproject.org/gdeltv2/"
SAFETY = timedelta(hours=1)
_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>")


def file_url(ts: datetime) -> str:
    return f"{BASE}{ts:%Y%m%d%H%M%S}.gkg.csv.zip"


def file_times(t: datetime, days: int = 3, per_hour: int = 2) -> list[datetime]:
    """Метки файлов в окне [t − days, t − SAFETY], по `per_hour` в час (минуты 00, 30 при per_hour=2)."""
    step = 60 // per_hour
    end = t - SAFETY
    h = end.replace(minute=0, second=0, microsecond=0)
    out = []
    cur = h + timedelta(minutes=60 - step)
    while cur > end:
        cur -= timedelta(minutes=step)
    lo = t - timedelta(days=days)
    while cur >= lo:
        out.append(cur)
        cur -= timedelta(minutes=step)
    return sorted(out)


def parse_gkg(raw: bytes) -> Iterator[Article]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            for line in z.read(name).decode("utf-8", "replace").splitlines():
                c = line.split("\t")
                if len(c) < 27:
                    continue
                try:
                    seen = datetime.strptime(c[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                m = _TITLE.search(c[26])
                title = html.unescape(m.group(1)).strip() if m else ""
                if title:
                    yield Article(seen, title, c[4], c[3], "")


def score(title: str, words: list[str]) -> int:
    low = re.findall(r"[a-z0-9]+", title.lower())
    have = set(low)
    return sum(1 for w in words if w.lower().strip(".'") in have)


def min_score(words: list[str]) -> int:
    return 1 if len(words) <= 1 else 2


class Collector:
    """Подбор заголовков к точкам (рынок × t): по каждой — top-N по числу совпавших слов, затем по свежести."""

    def __init__(self, keep: int = 30):
        self.keep = keep
        self.items: dict[str, list[tuple[int, Article]]] = {}

    def add(self, key: str, art: Article, t: datetime, words: list[str]) -> None:
        if art.seen >= t:  # строгая отсечка по строке — независимо от выбора файлов
            return
        s = score(art.title, words)
        if s < min_score(words):
            return
        lst = self.items.setdefault(key, [])
        if any(a.title == art.title for _, a in lst):
            return
        lst.append((s, art))
        lst.sort(key=lambda x: (x[0], x[1].seen), reverse=True)
        del lst[self.keep:]

    def articles(self, key: str) -> list[Article]:
        return sorted((a for _, a in self.items.get(key, [])), key=lambda a: a.seen, reverse=True)


def words_for(question: str) -> list[str]:
    return keywords(question, k=6)


def plan(points: list[tuple[str, str, datetime]], days: int = 3, per_hour: int = 2
         ) -> dict[datetime, list[tuple[str, datetime, list[str]]]]:
    """Файл → какие точки (key, t, слова) он обслуживает. Каждый файл качается один раз на все рынки."""
    by_file: dict[datetime, list[tuple[str, datetime, list[str]]]] = {}
    for key, question, t in points:
        w = words_for(question)
        if not w:
            continue
        for ts in file_times(t, days, per_hour):
            by_file.setdefault(ts, []).append((key, t, w))
    return by_file


def fetch(ts: datetime) -> Optional[bytes]:
    from forecast_bot.polymarket.http import get_bytes

    return get_bytes(file_url(ts))
