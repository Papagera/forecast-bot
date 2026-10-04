"""Multiple choice: явные названия в промпте, детерминированный разбор (в т.ч. заглушек Option_X по порядку)."""
from __future__ import annotations

import asyncio

import pytest
from fakes import FakeLlm, FakeMetaculusClient, questions

from forecast_bot import mc

HOUSE = ["Democrats", "Republicans", "Other"]
BALLON = ["Ousmane Dembélé", "Harry Kane", "Khvicha Kvaratskhelia", "Kylian Mbappé", "Rodri", "Lamine Yamal", "Other"]


def test_placeholders_map_by_position_real_house_case():
    # реальный ответ Opus 5.5 high (замер 04.10, вариант S): рассуждение за демократов, буквы вместо названий
    text = "Rationale: environment strongly favors a Democratic takeover.\n\nOption_A: 0.85\nOption_B: 0.14\nOption_C: 0.01"
    got = mc.parse_final(text, HOUSE)
    assert got["Democrats"] == pytest.approx(0.85, abs=0.01) and got["Republicans"] == pytest.approx(0.14, abs=0.01)


def test_placeholders_map_by_position_real_ballon_case():
    text = "\n".join(f"Option_{c}: {v}" for c, v in zip("ABCDEFG", (0.04, 0.09, 0.02, 0.26, 0.13, 0.39, 0.07)))
    got = mc.parse_final(text, BALLON)
    assert max(got, key=got.get) == "Lamine Yamal" and got["Ousmane Dembélé"] == pytest.approx(0.04, abs=0.01)


def test_names_with_percent_and_markdown():
    text = "Final:\n- **Democrats**: 84%\n- Republicans: 15%\n- Other: 1%"
    got = mc.parse_final(text, HOUSE)
    assert got == pytest.approx({"Democrats": 0.84, "Republicans": 0.15, "Other": 0.01}, abs=0.006)


def test_last_complete_block_wins():
    text = "Draft:\nDemocrats: 60%\nRepublicans: 35%\nOther: 5%\n\nAfter more thought:\nDemocrats: 80%\nRepublicans: 19%\nOther: 1%"
    assert mc.parse_final(text, HOUSE)["Democrats"] == pytest.approx(0.80, abs=0.01)


@pytest.mark.parametrize("text", [
    "Democrats: 80%\nRepublicans: 19%",                     # не все варианты
    "Democrats: 80%\nRepublicans: 80%\nOther: 80%",          # сумма далеко от 100%
    "no final answer here",
])
def test_ambiguous_returns_none(text):
    assert mc.parse_final(text, HOUSE) is None


def test_floor_keeps_every_option_positive():
    got = mc.parse_final("Democrats: 100%\nRepublicans: 0%\nOther: 0%", HOUSE)
    assert min(got.values()) >= mc.FLOOR * 0.99 and sum(got.values()) == pytest.approx(1.0)


def test_template_prompt_still_contains_placeholder_block():
    """Если шаблон поменяют, замена молча перестанет работать — ловим это здесь."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "vendor" / "metac_bot_template" / "main.py").read_text()
    import textwrap

    block = textwrap.dedent("\n".join(l for l in src.splitlines() if "Option_" in l or l.strip() == "..."))
    assert all(line in block for line in mc.TEMPLATE_PLACEHOLDER.splitlines())


def test_bot_sends_explicit_names_and_parses_without_llm_parser(fake_llm, fake_asknews, monkeypatch):
    from forecast_bot import paths, run as R
    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    original = FakeLlm._answer

    def answer(text):
        if "Option_A: Probability_A" in text:
            return "SHOULD NOT SEE PLACEHOLDER PROMPT"
        if "XX%" in text and "Альфа" in text:
            return "Рассуждение.\nOption_A: 0.6\nOption_B: 0.3\nOption_C: 0.1"
        return original(text)

    monkeypatch.setattr(FakeLlm, "_answer", staticmethod(answer))
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    mcq = [q for q in questions() if type(q).__name__ == "MultipleChoiceQuestion"]
    client = FakeMetaculusClient(mcq)
    asyncio.run(R.run(client=client, bot=ForecastBot(), journal=Journal(paths.journal_db()),
                      tournaments=["t"], submit=True))
    (qid, payload), = client.predictions
    probs = payload["probability_yes_per_category"]
    assert probs["Альфа"] == pytest.approx(0.6, abs=0.01) and probs["Гамма"] == pytest.approx(0.1, abs=0.01)
    assert not any("You are a data analyst helping to convert text" in p for p in fake_llm.prompts)
