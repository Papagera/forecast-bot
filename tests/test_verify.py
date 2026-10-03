"""Сверка чисел исследования с источниками: выдуманное не доходит до прогнозиста и считается."""
from __future__ import annotations

import asyncio

import pytest

from forecast_bot import verify

SOURCES = {
    "S0": "Will unemployment exceed 4.5% by December 2026?",
    "S1": "BLS: the unemployment rate was 4.2 percent in August 2026; payrolls rose by 29,000.",
    "S2": "Forbes estimated net worth at $7.3 billion in September 2025.",
}


def test_number_found_in_cited_source_passes_with_excerpt():
    v = verify.check("- Unemployment was 4.2% in August 2026 [S1]", SOURCES)
    assert v.numbers_unverified == 0 and v.facts_dropped == 0
    assert "4.2% in August 2026 [S1]" in v.text and "[S1] …" in v.text


def test_thousands_separator_normalized():
    v = verify.check("- Payrolls rose by 29000 [S1]", SOURCES)
    assert v.numbers_unverified == 0


def test_invented_number_drops_whole_fact():
    v = verify.check("- Unemployment was 4.3% in August 2026 [S1]\n- Net worth $7.3 billion [S2]", SOURCES)
    assert v.numbers_total == 3 and v.numbers_unverified == 1 and v.facts_dropped == 1  # 4.3, 2026, 7.3
    assert "4.3%" not in v.text.split("Note:")[0] and "$7.3 billion" in v.text
    assert v.dropped == ["- Unemployment was 4.3% in August 2026 [S1]"]


def test_number_without_citation_is_dropped():
    v = verify.check("- Payrolls rose by 29,000", SOURCES)
    assert v.numbers_unverified == 1 and v.facts_dropped == 1


def test_number_from_wrong_source_is_dropped():
    v = verify.check("- Net worth $7.3 billion [S1]", SOURCES)
    assert v.facts_dropped == 1


def test_question_numbers_allowed_without_citation():
    v = verify.check("- Threshold is 4.5% by December 2026", SOURCES)
    assert v.numbers_unverified == 0


def test_partial_match_is_not_a_match():
    assert verify._find("4.2", "rate 14.25 percent") == -1
    assert verify._find("4.2", "rate 4.20 percent") >= 0


def test_small_integers_and_citation_ids_not_counted():
    assert verify.numbers_in("- 3 options, see [S12] and [S1, S3]") == []


def test_unsupported_numbers_for_forecaster():
    total, missing = verify.unsupported_numbers("Rate is 4.2 and maybe 5.9", "rate 4.2")
    assert (total, missing) == (2, 1)


def test_agent_hallucination_is_dropped_and_journaled(monkeypatch, fake_llm, fake_asknews):
    from fakes import FakeMetaculusClient, questions

    from forecast_bot import agent as A, paths, run as R
    from forecast_bot.bot import ForecastBot
    from forecast_bot.journal import Journal

    async def fake_news(self, query):
        return "**Новость** без чисел"

    monkeypatch.setattr(ForecastBot, "_asknews_latest", fake_news)
    monkeypatch.setattr(A, "fred_series", lambda sid: f"FRED {sid}: последнее 4.1")
    monkeypatch.setenv("FORECAST_RESEARCH", "agent")
    monkeypatch.setenv("FORECAST_PREDICTIONS", "1")
    fake_llm.hallucinate = True
    journal = Journal(paths.journal_db())
    result = asyncio.run(R.run(client=FakeMetaculusClient(questions()[:1]), bot=ForecastBot(), journal=journal,
                               tournaments=["t"], submit=False, variant="A"))
    row = journal.rows(result.run_id)[0]
    assert row["research_numbers"] == 2 and row["research_unverified"] == 1 and row["variant"] == "A"
    assert "77.7" in row["research_dropped"]
    forecast_prompt = next(p for p in fake_llm.prompts if '"Probability: ZZ%"' in p)
    assert "4.1" in forecast_prompt and "77.7" not in forecast_prompt
    assert row["forecast_numbers"] is not None
