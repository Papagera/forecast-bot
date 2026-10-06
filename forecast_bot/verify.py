"""Сверка чисел с источниками (блок 2.1): «число без источника не передаётся прогнозисту».

Агент пишет справку строками-фактами со ссылками на источники `[S#]` (S0 — сам вопрос, S1… — ответы
инструментов). Здесь каждое число факта ищется в тексте процитированных источников (плюс S0):
- нашлось везде → факт проходит, к нему прикладывается выдержка из источника;
- хоть одно число не нашлось или ссылок нет → факт целиком отбрасывается и считается «непроверенным».
Сверка буквальная (после нормализации разделителей тысяч): «2.6B» в справке и «2,600,000,000» в источнике —
это промах. Так строже, чем нужно, но ложный пропуск выдуманного числа дороже ложного отброса.

Не считаются числами: id ссылок `[S3]` и голые целые 0–12 (счётные слова, месяцы, номера пунктов).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_CITE_RE = re.compile(r"\[(S\d+(?:\s*[,;]\s*S\d+)*)\]")
_NUM_RE = re.compile(r"(?<![\w.])[-−]?\$?(\d[\d,]*(?:\.\d+)?)\s?%?")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")
EXCERPT_RADIUS = 120
EXCERPTS_LIMIT = 3000


def normalize(text: str) -> str:
    return _THOUSANDS_RE.sub("", text)


def numbers_in(text: str) -> list[str]:
    """Значимые числа строки (без ссылок [S#] и голых целых 0–12)."""
    text = _CITE_RE.sub(" ", normalize(text))
    out = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(1).rstrip(".,")
        if re.fullmatch(r"\d+", raw) and int(raw) <= 12:
            continue
        num = raw.rstrip("0").rstrip(".") if "." in raw else raw
        out.append(num or "0")
    return out


def _find(num: str, source: str) -> int:
    """Позиция числа в источнике как отдельного числа («4.2» не находится в «14.25»)."""
    m = re.search(rf"(?<![\d.]){re.escape(num)}(?:0*)(?![\d])", source)
    return m.start() if m else -1


def cited_ids(line: str) -> set[str]:
    ids: set[str] = set()
    for group in _CITE_RE.findall(line):
        ids.update(re.findall(r"S\d+", group))
    return ids


@dataclass
class Verdict:
    text: str                     # что уходит прогнозисту
    numbers_total: int = 0
    numbers_unverified: int = 0
    facts_dropped: int = 0
    dropped: list[str] = field(default_factory=list)
    facts_cited: int = 0                                  # оставленных строк справки со ссылкой [S#] (не на вопрос)
    cited_sources: set = field(default_factory=set)       # какие источники процитированы в оставленных строках

    @property
    def unverified_share(self) -> float:
        return self.numbers_unverified / self.numbers_total if self.numbers_total else 0.0


def check(brief: str, sources: dict[str, str]) -> Verdict:
    """`sources` — {"S0": текст вопроса, "S1": ответ инструмента, …}."""
    norm = {k: normalize(v) for k, v in sources.items()}
    kept, excerpts, dropped = [], [], []
    total = unverified = 0
    excerpt_len = 0
    cited_kept, cited_src = 0, set()

    def keep(line: str) -> None:
        nonlocal cited_kept
        kept.append(line)
        ids = (cited_ids(line) & set(norm)) - {"S0"}
        if ids:
            cited_kept += 1
            cited_src.update(ids)

    for line in brief.splitlines():
        nums = numbers_in(line)
        if not nums:
            keep(line)
            continue
        total += len(nums)
        ids = (cited_ids(line) & set(norm)) | {"S0"}
        found: list[tuple[str, str, int]] = []
        missing = 0
        for num in nums:
            hit = next(((sid, pos) for sid in sorted(ids) if (pos := _find(num, norm[sid])) >= 0), None)
            if hit is None:
                missing += 1
            else:
                found.append((num, hit[0], hit[1]))
        if missing:
            unverified += missing
            dropped.append(line.strip())
            continue
        keep(line)
        for num, sid, pos in found:
            if sid == "S0" or excerpt_len >= EXCERPTS_LIMIT:
                continue
            src = norm[sid]
            snippet = src[max(0, pos - EXCERPT_RADIUS): pos + EXCERPT_RADIUS].replace("\n", " ")
            excerpts.append(f"[{sid}] …{snippet}…")
            excerpt_len += len(snippet)
    body = "\n".join(kept).strip()
    parts = ["Verified research facts (every number below was found in the cited source text):", body]
    if excerpts:
        parts += ["", "Source excerpts — check each key number against its excerpt and ignore any fact the "
                      "excerpt does not support:", *dict.fromkeys(excerpts)]
    if dropped:
        parts += ["", f"Note: {len(dropped)} fact(s) were removed because their numbers could not be found in "
                      "the cited sources. Do not reintroduce such numbers."]
    return Verdict("\n".join(parts), total, unverified, len(dropped), dropped, cited_kept, cited_src)


def unsupported_numbers(text: str, corpus: str) -> tuple[int, int]:
    """(всего чисел, не найдено в корпусе) — для рассуждения прогнозиста против исследования+вопроса."""
    nums = numbers_in(text)
    src = normalize(corpus)
    return len(nums), sum(1 for n in nums if _find(n, src) < 0)
