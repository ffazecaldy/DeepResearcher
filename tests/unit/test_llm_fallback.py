"""Tests for the LLM fallback DecisionEngine (fake LLM)."""
from __future__ import annotations

import json

import pytest

from app.decision.base import DecisionUnavailableError
from app.decision.llm_fallback import LLMDecisionEngine
from app.models import DecisionKind, DecisionSpec
from tests.fakes import FakeLLMClient

BINARY = DecisionSpec(name="is_relevant", kind=DecisionKind.BINARY,
                      question="Relevant?")
CHOICE = DecisionSpec(name="source_type", kind=DecisionKind.CHOICE,
                      question="Type?", options={"news": "articles",
                                                 "paper": "research"})
SCORE = DecisionSpec(name="quality", kind=DecisionKind.SCORE,
                     question="Quality?", scale=["low", "medium", "high"])


def _resp(value, confidence=0.8) -> str:
    return json.dumps({"value": value, "confidence": confidence})


async def test_binary_true():
    eng = LLMDecisionEngine(FakeLLMClient([_resp(True, 0.9)]))
    res = await eng.decide(BINARY, "state text")
    assert res.engine == "llm"
    assert res.value is True
    assert res.confidence == pytest.approx(0.9)
    assert res.abstained is False
    assert res.subject_id == ""


async def test_binary_string_coercion():
    eng = LLMDecisionEngine(FakeLLMClient([_resp("SÌ")]))
    res = await eng.decide(BINARY, "state")
    assert res.value is True


async def test_choice_exact_and_case_insensitive():
    eng = LLMDecisionEngine(FakeLLMClient([_resp("news"), _resp("  PAPER ")]))
    r1 = await eng.decide(CHOICE, "state")
    r2 = await eng.decide(CHOICE, "state")
    assert r1.value == "news"
    assert r2.value == "paper"  # normalized to the exact option label


async def test_score_in_range():
    eng = LLMDecisionEngine(FakeLLMClient([_resp(2, 0.7)]))
    res = await eng.decide(SCORE, "state")
    assert res.value == 2
    assert res.confidence == pytest.approx(0.7)


async def test_invalid_choice_raises_unavailable():
    eng = LLMDecisionEngine(FakeLLMClient([_resp("unknown_option")]))
    with pytest.raises(DecisionUnavailableError, match="source_type"):
        await eng.decide(CHOICE, "state")


async def test_score_out_of_range_raises():
    eng = LLMDecisionEngine(FakeLLMClient([_resp(9)]))
    with pytest.raises(DecisionUnavailableError):
        await eng.decide(SCORE, "state")


async def test_llm_crash_raises_unavailable():
    class Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("api down")

    eng = LLMDecisionEngine(Boom())
    with pytest.raises(DecisionUnavailableError, match="api down"):
        await eng.decide(BINARY, "state")


async def test_state_truncated_and_json_forced():
    fake = FakeLLMClient([_resp(True)])
    eng = LLMDecisionEngine(fake)
    await eng.decide(BINARY, "x" * 20000)
    assert len(fake.last_user) < 7000
    assert fake.calls[0]["force_json"] is True
