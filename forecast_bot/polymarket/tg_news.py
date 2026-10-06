"""Бэктест №3: посты из Telegram-папки Никиты как источник новостей «как будто на дату t». Только чтение.

Доступ (подтверждён Никитой в сессии 06.10.2026): Telethon, сессия-строка `~/.config/income/tg.session` (общая с
clipper — только читаем, никогда не перезаписываем), ключи TG_API_ID/TG_API_HASH — копия в
`~/.forecast-bot/polymarket/tg/tg.env` (600). Читается ТОЛЬКО папка с заданным id и в ней ТОЛЬКО каналы
(broadcast); личные чаты, группы и всё вне папки не открываются.

Тексты постов — только локально (`~/.forecast-bot/polymarket/tg/`, вне репозитория). В репозиторий и в отчёт не
попадает ни текст постов, ни названия каналов: в данных канал — числовой id, в отчётах — порядковый номер.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

UTC = timezone.utc
SESSION_PATH = Path("~/.config/income/tg.session").expanduser()
EXCERPT = 280      # символов поста в промпт прогнозиста
KEEP = 20          # постов на точку
DAYS = 3           # окно до t (как у GDELT)


def tg_dir() -> Path:
    from forecast_bot.polymarket import backtest as B

    return B.data_dir() / "tg"


@dataclass
class Post:
    channel: int       # числовой id канала (не название)
    msg: int
    date: datetime
    text: str

    def to_json(self) -> str:
        return json.dumps({"channel": self.channel, "msg": self.msg, "date": self.date.isoformat(),
                           "text": self.text}, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Post":
        d = json.loads(line)
        return cls(int(d["channel"]), int(d["msg"]), datetime.fromisoformat(d["date"]), d["text"])


class TgSetupError(RuntimeError):
    """Нет ключей / сессии / папки — причина без значений секретов."""


def credentials(path: Optional[Path] = None) -> tuple[int, str]:
    p = path or tg_dir() / "tg.env"
    vals = {}
    if p.exists():
        for line in p.read_text().splitlines():
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip().strip('"')
    api_id, api_hash = vals.get("TG_API_ID", ""), vals.get("TG_API_HASH", "")
    if not api_id.isdigit() or not api_hash:
        raise TgSetupError(f"нет TG_API_ID/TG_API_HASH в {p}")
    return int(api_id), api_hash


def session_string(path: Path = SESSION_PATH) -> str:
    s = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not s:
        raise TgSetupError(f"нет сессии {path}")
    return s


async def folder_channels(client, folder_id: int) -> list:
    """Каналы (broadcast) папки `folder_id`. Папки нет → ошибка; не-каналы папки пропускаются."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest

    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res)
    folder = next((f for f in filters if getattr(f, "id", None) == folder_id), None)
    if folder is None:
        raise TgSetupError(f"папки {folder_id} нет")
    out = []
    for peer in getattr(folder, "include_peers", []) or []:
        ent = await client.get_entity(peer)
        if getattr(ent, "broadcast", False):
            out.append(ent)
    return out


async def fetch(client, folder_id: int, since: datetime, until: datetime) -> list[Post]:
    """Посты каналов папки с since ≤ дата < until. Ничего, кроме этих каналов, не читается."""
    posts: list[Post] = []
    for ent in await folder_channels(client, folder_id):
        async for msg in client.iter_messages(ent, offset_date=until):
            d = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=UTC)
            if d < since:
                break
            if d >= until:
                continue
            text = (getattr(msg, "message", None) or "").strip()
            if text:
                posts.append(Post(int(ent.id), int(msg.id), d, text))
    return posts


def load_posts(path: Optional[Path] = None) -> list[Post]:
    p = path or tg_dir() / "posts.jsonl"
    if not p.exists():
        return []
    return [Post.from_json(l) for l in p.read_text().splitlines() if l.strip()]


# ─────────────────────────── отбор к точке ─────────────────────────
def _norm(s: str) -> str:
    return s.lower().replace("ё", "е").replace("’", "'").replace("ʼ", "'")


def score(text: str, words: Iterable[str]) -> int:
    """Число ключевых слов (основ), встретившихся в посте. Основа — префикс: «київ» ловит «києві» не всегда,
    поэтому ключи даются основами («киє», «київ», «киев»)."""
    t = _norm(text)
    return sum(1 for w in words if w and _norm(w) in t)


def select(posts: list[Post], t: datetime, words: list[str], days: int = DAYS, keep: int = KEEP,
           min_score: int = 1) -> list[Post]:
    """Посты строго ДО t (и не раньше t − days), по числу совпавших ключей, затем по свежести."""
    lo = t - timedelta(days=days)
    cand = [(score(p.text, words), p) for p in posts if lo <= p.date < t]
    cand = [(s, p) for s, p in cand if s >= min_score]
    cand.sort(key=lambda x: (x[0], x[1].date), reverse=True)
    return sorted((p for _, p in cand[:keep]), key=lambda p: p.date, reverse=True)


def as_research(posts: list[Post], channel_no: dict[int, int]) -> str:
    """Выжимка для прогнозиста: дата, номер канала (не название), первые EXCERPT символов."""
    if not posts:
        return "No relevant Telegram posts were found before the forecast date."
    lines = ["Recent posts from Ukrainian Telegram news channels (Ukrainian/Russian), seen before the forecast date, "
             "newest first:"]
    for i, p in enumerate(posts, 1):
        body = re.sub(r"\s+", " ", p.text)[:EXCERPT]
        lines.append(f"[T{i}] {p.date:%Y-%m-%d %H:%M} UTC · channel {channel_no.get(p.channel, 0)} · {body}")
    return "\n".join(lines)
