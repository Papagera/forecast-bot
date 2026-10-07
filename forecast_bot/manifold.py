"""Бэктест бота на закрытых рынках Manifold (07.10.2026): бот против толпы «как будто на дату t». Только чтение.

Metaculus API не отдаёт исход и историю прогноза сообщества закрытых вопросов ни боту, ни личному токену (проверено
07.10.2026: resolution = None, aggregations.history пуст; HTML — 403 Cloudflare) → запасной план — Manifold:
публичный API отдаёт исход (YES/NO/CANCEL) и ставки с `probAfter`, из них — вероятность толпы на любую дату t.

Утечка будущего — главный риск:
- точка t = плановое закрытие − 7 дней; рынок, решённый до t, — вон;
- вероятность толпы — последняя ставка СТРОГО до t;
- описание рынка авторы дописывают задним числом («resolved because…») — его проверяет дешёвая модель до прогноза,
  с упоминанием исхода — вон; справку агента — регулярка на даты после t + та же модель;
- новости — заголовки GDELT строго до t; ряды FRED/Yahoo обрезаются датой t; fetch_url (страница «сегодня») выключен.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc
API = "https://api.manifold.markets/v0"
HORIZON = timedelta(days=7)
MIN_BETTORS = 15
MIN_VOLUME = 1000.0
MIN_BETS_BEFORE_T = 3
DESC_LIMIT = 1500


def ms_to_dt(ms) -> Optional[datetime]:
    return datetime.fromtimestamp(ms / 1000, UTC) if ms else None


@dataclass
class MfMarket:
    id: str
    question: str
    description: str
    created: datetime
    close: datetime          # плановое закрытие
    resolved_at: Optional[datetime]
    outcome: Optional[int]   # 1 YES, 0 NO, None — отменён/не бинарный исход
    bettors: int
    volume: float
    groups: list = field(default_factory=list)
    url: str = ""

    @property
    def t(self) -> datetime:
        return self.close - HORIZON

    @property
    def group(self) -> str:
        return self.groups[0] if self.groups else "без группы"


def from_api(m: dict) -> Optional[MfMarket]:
    if m.get("outcomeType") not in (None, "BINARY") or m.get("mechanism") not in (None, "cpmm-1"):
        return None
    res = m.get("resolution")
    outcome = {"YES": 1, "NO": 0}.get(res)
    if outcome is None:
        return None  # CANCEL / MKT — не бинарный итог
    created, close = ms_to_dt(m.get("createdTime")), ms_to_dt(m.get("closeTime"))
    if not created or not close:
        return None
    desc = m.get("textDescription") or ""
    return MfMarket(id=str(m["id"]), question=m.get("question") or "", description=desc[:DESC_LIMIT], created=created,
                    close=close, resolved_at=ms_to_dt(m.get("resolutionTime")), outcome=outcome,
                    bettors=int(m.get("uniqueBettorCount") or 0), volume=float(m.get("volume") or 0),
                    groups=list(m.get("groupSlugs") or []), url=m.get("url") or "")


def eligible(m: MfMarket, close_min: datetime, close_max: datetime) -> Optional[str]:
    """None — годен; иначе причина отбора."""
    if not (close_min <= m.close <= close_max):
        return "закрытие вне окна"
    if m.bettors < MIN_BETTORS or m.volume < MIN_VOLUME:
        return "мало торгов"
    if m.created > m.t - timedelta(days=1):
        return "создан позже t − 1 сут"
    if m.resolved_at and m.resolved_at <= m.t:
        return "решён до t"
    return None


def prob_at(bets: list[dict], t: datetime) -> Optional[tuple[float, int]]:
    """(вероятность толпы, ставок до t): probAfter последней ставки СТРОГО до t."""
    ts = t.timestamp() * 1000
    before = sorted((b for b in bets if b.get("createdTime") and b["createdTime"] < ts
                     and b.get("probAfter") is not None and not b.get("isRedemption")),
                    key=lambda b: b["createdTime"])
    if not before:
        return None
    return float(before[-1]["probAfter"]), len(before)


def sample(ms: list[MfMarket], n: int, per_group: int = 6) -> list[MfMarket]:
    """До n рынков, не больше per_group на первую группу; порядок — хэш id (не исход, не цена)."""
    out, cnt = [], {}
    for m in sorted(ms, key=lambda m: zlib.crc32(m.id.encode())):
        if cnt.get(m.group, 0) >= per_group:
            continue
        cnt[m.group] = cnt.get(m.group, 0) + 1
        out.append(m)
        if len(out) >= n:
            break
    return out


# ─────────────────────────── утечка в тексте ────────────────────────
_MON = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
_DATES = [re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b"),
          re.compile(rf"\b{_MON}\s+(\d{{1,2}}),?\s+(20\d\d)\b", re.I),
          re.compile(rf"\b(\d{{1,2}})\s+{_MON}\s+(20\d\d)\b", re.I)]
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}


def dates_in(text: str) -> list[date]:
    out = []
    for m in _DATES[0].finditer(text or ""):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in _DATES[1].finditer(text or ""):
        try:
            out.append(date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
        except (ValueError, KeyError):
            pass
    for m in _DATES[2].finditer(text or ""):
        try:
            out.append(date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
        except (ValueError, KeyError):
            pass
    return out


def dates_after(text: str, t: datetime, close: datetime, known: str = "") -> list[date]:
    """Даты в тексте позже t. Плановое закрытие и даты из вопроса/описания (`known`) — не утечка: их знали на t."""
    allowed = {close.date(), *dates_in(known)}
    return [d for d in dates_in(text) if d > t.date() and d not in allowed]


LEAK_PROMPT = """Forecast date: {t}. Below is text that a forecaster would see on that date.
Question: {q}
Text:
{text}

Does the text reveal how the question resolved, or describe specific events that happened AFTER {t}
(not scheduled/expected events, but things reported as already happened)? Answer JSON only:
{{"leak": true|false, "why": "short"}}"""
