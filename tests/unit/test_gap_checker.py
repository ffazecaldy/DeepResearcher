"""Tests for GapChecker sanitization (fake LLM, tmp prompts dir)."""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.gap_checker import GapChecker
from app.models import SubQuestion
from tests.fakes import FakeLLMClient

RAW = {
    "complete": False,
    "covered_subquestions": ["sq1", "GHOST"],
    "missing_subquestions": ["sq2", "GHOST"],
    "contradictions": ["A says X, B says Y"],
    "new_queries": [
        {"subquestion_id": "sq2", "text": "brevetto telefono  1876"},
        {"subquestion_id": "sq2", "text": "brevetto   telefono 1876"},  # near-dup
        {"subquestion_id": "GHOST", "text": "referenza inesistente"},   # bad sid
        {"subquestion_id": "sq1", "text": ""},                          # empty
        "not-a-dict",
    ],
}


def _tmp_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "gap_checker.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} SQ {subquestions_json} EV {evidence_json}\n",
        encoding="utf-8")
    return d


def _subs() -> list[SubQuestion]:
    return [SubQuestion(subquestion_id="sq1", text="Chi?", idx=1),
            SubQuestion(subquestion_id="sq2", text="Quando?", idx=2)]


async def test_sanitize_and_dedup(tmp_path):
    fake = FakeLLMClient([json.dumps(RAW)])
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))

    report = await gc.check("Storia del telefono?", _subs(), [])

    assert report.complete is False
    assert report.covered_subquestions == ["sq1"]   # GHOST dropped
    assert report.missing_subquestions == ["sq2"]   # GHOST dropped
    assert report.contradictions == ["A says X, B says Y"]
    assert len(report.new_queries) == 1             # dup/bad/empty dropped
    assert report.new_queries[0].subquestion_id == "sq2"

    # placeholders formatted
    assert "Storia del telefono?" in fake.last_user
    assert fake.calls[0]["force_json"] is True


async def test_complete_report(tmp_path):
    raw = {"complete": True, "covered_subquestions": ["sq1", "sq2"],
           "missing_subquestions": [], "contradictions": [], "new_queries": []}
    fake = FakeLLMClient([json.dumps(raw)])
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))

    report = await gc.check("q", _subs(), [])
    assert report.complete is True
    assert report.new_queries == []


async def test_invalid_json_raises(tmp_path):
    fake = FakeLLMClient(["definitely not json"])
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))

    from app.agent.gap_checker import GapCheckError
    try:
        await gc.check("q", _subs(), [])
        raise AssertionError("expected GapCheckError")
    except GapCheckError:
        pass
