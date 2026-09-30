"""Tests for the Laya adapter (FakeRouter, no laya dependency required)."""
from __future__ import annotations

import sys

import pytest

from app.decision.base import DecisionUnavailableError
from app.decision.laya import LayaDecisionEngine
from app.models import DecisionKind, DecisionSpec


class FakeRouter:
    """README-shaped predict() double covering the three decision types."""

    def __init__(self, *, low_confidence: bool = False, fail: bool = False):
        self.calls = 0
        self.low_confidence = low_confidence
        self.fail = fail

    def predict(self, state, questions, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        answers = {}
        for name, q in questions.items():
            if q["type"] == "noul":
                answers[name] = {"noul": 0.92,
                                 "low_confidence": self.low_confidence}
            elif q["type"] == "choice":
                answers[name] = {"choice": "news", "answer_confidence": 0.8,
                                 "low_confidence": self.low_confidence}
            else:
                answers[name] = {"score": 2, "confidence": 0.7,
                                 "low_confidence": self.low_confidence}
        return {"answers": answers, "routing": {"model": "fake"}}


def _engine(router: FakeRouter) -> LayaDecisionEngine:
    eng = LayaDecisionEngine(min_confidence=0.35)
    eng._router = router  # bypass lazy import
    return eng


BINARY = DecisionSpec(name="is_relevant", kind=DecisionKind.BINARY,
                      question="Is this document relevant?")
CHOICE = DecisionSpec(name="source_type", kind=DecisionKind.CHOICE,
                      question="What type?", options={"news": "articles",
                                                      "paper": "research"})
SCORE = DecisionSpec(name="quality", kind=DecisionKind.SCORE,
                     question="Rate quality.", scale=["low", "medium", "high"])


async def test_binary_decision():
    res = await _engine(FakeRouter()).decide(BINARY, "state text")
    assert res.engine == "laya"
    assert res.value is True
    assert res.confidence == pytest.approx(0.92)
    assert res.abstained is False


async def test_choice_decision():
    res = await _engine(FakeRouter()).decide(CHOICE, "state text")
    assert res.value == "news"
    assert res.confidence == pytest.approx(0.8)


async def test_score_decision():
    res = await _engine(FakeRouter()).decide(SCORE, "state text")
    assert res.value == 2
    assert res.confidence == pytest.approx(0.7)


async def test_low_confidence_abstains():
    res = await _engine(FakeRouter(low_confidence=True)).decide(BINARY, "state")
    assert res.abstained is True
    assert res.value is None


async def test_batch_single_forward_pass():
    router = FakeRouter()
    results = await _engine(router).decide_many([BINARY, CHOICE, SCORE], "state")
    assert router.calls == 1  # all questions in ONE predict call
    assert [r.value for r in results] == [True, "news", 2]


async def test_failure_raises_unavailable():
    with pytest.raises(DecisionUnavailableError):
        await _engine(FakeRouter(fail=True)).decide(BINARY, "state")


async def test_batch_failure_returns_none():
    res = await _engine(FakeRouter(fail=True)).decide_many([BINARY], "state")
    assert res is None


async def test_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "laya", None)  # forces ImportError on lazy import
    eng = LayaDecisionEngine()
    with pytest.raises(DecisionUnavailableError, match="not installed"):
        await eng.decide(BINARY, "state")
