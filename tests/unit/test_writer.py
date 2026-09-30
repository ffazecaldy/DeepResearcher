"""Tests for Writer post-processing (fake LLM, tmp prompts)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.writer import Writer, WriterError
from app.models import Evidence, SubQuestion
from tests.fakes import FakeLLMClient

REPORT = {
    "title": "Telefono",
    "markdown": "Frase uno.\n\nFrase due.",
    "claims": [
        {"text": "Frase uno.", "citation_no": 9, "evidence_ids": ["ev1"]},
        {"text": "Frase due.", "citation_no": 3, "evidence_ids": ["evGHOST"]},
        {"text": "Frase tre.", "citation_no": 4, "evidence_ids": []},
    ],
}


def _tmp_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "writer.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} L {language} EV {evidence_json} "
        "SQ {subquestions_json}\n", encoding="utf-8")
    return d


def _ev(eid: str = "ev1") -> Evidence:
    return Evidence(evidence_id=eid, source_id="src1", chunk_id="chk1",
                    quote="quote", claim="claim", run_id="r1")


def _subs() -> list[SubQuestion]:
    return [SubQuestion(subquestion_id="sq1", text="Chi?", idx=1)]


async def test_drops_unknown_evidence_and_renumbers(tmp_path):
    fake = FakeLLMClient([json.dumps(REPORT)])
    w = Writer(fake, prompts_dir=_tmp_prompts(tmp_path))

    draft = await w.write("domanda", "it", _subs(), [_ev()])

    assert len(draft.claims) == 1  # ghost + empty dropped
    assert draft.claims[0].citation_no == 1  # renumbered
    assert draft.claims[0].evidence_ids == ["ev1"]
    assert draft.title == "Telefono"


async def test_limit_note_appended(tmp_path):
    fake = FakeLLMClient([json.dumps(REPORT)])
    w = Writer(fake, prompts_dir=_tmp_prompts(tmp_path))

    draft = await w.write("domanda", "it", _subs(), [_ev()],
                          limit_note="max pages")
    assert "max pages" in draft.markdown


async def test_no_evidences_raises(tmp_path):
    w = Writer(FakeLLMClient([]), prompts_dir=_tmp_prompts(tmp_path))
    with pytest.raises(WriterError):
        await w.write("domanda", "it", _subs(), [])


async def test_invalid_json_retry_then_error(tmp_path):
    fake = FakeLLMClient(["nope", "still bad {"])
    w = Writer(fake, prompts_dir=_tmp_prompts(tmp_path))
    with pytest.raises(WriterError):
        await w.write("domanda", "it", _subs(), [_ev()])
    assert fake.call_count == 2
