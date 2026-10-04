"""Разбор финального ответа на вопрос multiple choice — детерминированно, без угадывания LLM-парсером.

Найдено 04.10.2026 на замере исследователей: Opus 5.5 high отвечает буквальными заглушками из промпта шаблона
(«Option_A: 0.85 / Option_B: 0.14 …») — так в журнале замеров ответили 15 из 17 Opus-прогнозов MC (варианты A/B/H/S/G). Шаблон отдавал
это LLM-парсеру (gpt-4o-mini), и тот сопоставлял буквы с вариантами наугад: «Democrats 85%» превращалось в
«Republicans 85%», «Lamine Yamal 39%» — в «Ousmane Dembélé 39%». Уверенная ставка не на тот вариант — худший
исход для log score.

Правило: в промпте просим точные названия; в ответе берём ПОСЛЕДНИЙ блок строк «вариант: число» — по названию,
а «Option_X» — по позиции X в списке вариантов (шаблон велит писать «in this order {options}»). Не удалось
разобрать однозначно → None, и тогда работает прежний путь шаблона (LLM-парсер).
"""
from __future__ import annotations

import re
from typing import Optional

TEMPLATE_PLACEHOLDER = "Option_A: Probability_A\nOption_B: Probability_B\n...\nOption_N: Probability_N"
FLOOR = 0.005  # минимальная вероятность варианта (Metaculus не любит нули; log score — тоже)

_LINE = re.compile(r"^\s*(?:[-*•]\s*)?\**(?P<label>[^:\n=]{1,120}?)\**\s*[:=]\s*\**(?P<num>\d+(?:\.\d+)?)\s*(?P<pct>%?)")
_LETTER = re.compile(r"^option[_ ]?([a-z])$", re.I)


def explicit_format(options: list[str]) -> str:
    lines = "\n".join(f"{o}: XX%" for o in options)
    return (f"{lines}\n(Write every option's exact name as shown, one per line, with probabilities summing to 100%. "
            f"Do not use placeholders like Option_A.)")


def patch_prompt(prompt: str, options: list[str]) -> str:
    """Заменить блок-заглушку шаблона на явный формат с названиями вариантов."""
    return prompt.replace(TEMPLATE_PLACEHOLDER, explicit_format(options))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip("*_`\"'").lower()


def parse_final(reasoning: str, options: list[str]) -> Optional[dict[str, float]]:
    """{вариант: вероятность} из последнего полного блока ответа, или None."""
    by_name = {_norm(o): o for o in options}
    blocks: list[tuple[dict[str, float], bool]] = []
    current: dict[str, float] = {}
    pct_seen = False
    for line in reasoning.splitlines():
        m = _LINE.match(line)
        opt = None
        if m:
            label = _norm(m.group("label"))
            lm = _LETTER.match(label)
            if lm:
                k = ord(lm.group(1).lower()) - ord("a")
                opt = options[k] if 0 <= k < len(options) else None
            else:
                opt = by_name.get(label)
        if opt is None:
            if current:
                blocks.append((current, pct_seen))
                current, pct_seen = {}, False
            continue
        val = float(m.group("num"))
        pct_seen = pct_seen or bool(m.group("pct")) or val > 1
        current[opt] = val
    if current:
        blocks.append((current, pct_seen))
    for block, pct in reversed(blocks):  # финальный ответ — последний полный блок
        if set(block) != set(options):
            continue
        vals = {k: (v / 100 if pct else v) for k, v in block.items()}
        total = sum(vals.values())
        if not 0.9 <= total <= 1.1:
            return None
        vals = {k: max(FLOOR, v / total) for k, v in vals.items()}
        s = sum(vals.values())
        return {k: v / s for k, v in vals.items()}
    return None
