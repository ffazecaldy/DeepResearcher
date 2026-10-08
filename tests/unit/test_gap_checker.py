"""Tests for GapChecker: code-decided coverage per sub-question."""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.gap_checker import GapChecker, compute_coverage
from app.models import Evidence, SubQuestion
from tests.fakes import FakeLLMClient

RAW = {
    "subquestion_status": [
        {"id": "sq1", "note_ids": ["ev1"], "motivo": "fonte diretta"},
        {"id": "sq2", "note_ids": [], "motivo": "nessuna nota"},
    ],
    "contradictions": ["A dice X, B dice Y"],
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


def _ev(eid: str, sid: str, source: str) -> Evidence:
    return Evidence(evidence_id=eid, run_id="r1", source_id=source,
                    chunk_id="chk", quote="q", claim="c", subquestion_id=sid)


async def test_llm_analysis_and_code_decides(tmp_path):
    fake = FakeLLMClient([json.dumps(RAW)])
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))
    evidences = [
        _ev("ev1", "sq1", "src_wiki"),
        _ev("ev2", "sq1", "src_news"),   # 2 DOMINI diversi -> sq1 coperta
        _ev("ev3", "sq1", "src_wiki2"),  # stesso dominio wiki -> non aggiunge
    ]

    report = await gc.check("Storia del telefono?", _subs(), evidences,
                            domain_of={"src_wiki": "wikipedia.org",
                                       "src_news": "corriere.it",
                                       "src_wiki2": "wikipedia.org"})

    # sq1: 2 domini indipendenti -> coperta; sq2: 0 -> non_coperta
    assert report.status_by_id == {"sq1": "coperta", "sq2": "non_coperta"}
    assert report.complete is False
    assert report.covered_subquestions == ["sq1"]
    assert report.missing_subquestions == ["sq2"]
    assert report.contradictions == ["A dice X, B dice Y"]
    assert len(report.new_queries) == 1  # dup/bad/empty dropped
    assert report.new_queries[0].subquestion_id == "sq2"

    # placeholders formatted
    assert "Storia del telefono?" in fake.last_user
    assert fake.calls[0]["force_json"] is True


async def test_same_domain_counts_as_one(tmp_path):
    fake = FakeLLMClient([json.dumps(RAW)])
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))
    evidences = [_ev("ev1", "sq1", "src_a"), _ev("ev2", "sq1", "src_b")]

    report = await gc.check("q", _subs(), evidences,
                            domain_of={"src_a": "stesso.it",
                                       "src_b": "stesso.it"})
    # stesso dominio = UNA sola fonte indipendente -> parziale
    assert report.status_by_id["sq1"] == "parziale"
    assert report.complete is False


async def test_complete_requires_all_covered(tmp_path):
    # P1-7: with an open contradiction the research stays open even if every
    # sub-question is covered -> RAW has contradictions, so build a clean one
    raw_clean = dict(RAW)
    raw_clean["contradictions"] = []
    fake = FakeLLMClient([json.dumps(raw_clean)])
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))
    evidences = [
        _ev("ev1", "sq1", "s1"), _ev("ev2", "sq1", "s2"),
        _ev("ev3", "sq2", "s3"), _ev("ev4", "sq2", "s4"),
    ]
    report = await gc.check("q", _subs(), evidences,
                            domain_of={f"s{i}": f"d{i}.it" for i in range(1, 5)})
    assert report.complete is True
    assert report.covered_subquestions == ["sq1", "sq2"]


async def test_open_contradiction_blocks_complete(tmp_path):
    """P1-7: covered subquestions + unresolved contradiction -> NOT complete."""
    fake = FakeLLMClient([json.dumps(RAW)])  # RAW carries one contradiction
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))
    evidences = [
        _ev("ev1", "sq1", "s1"), _ev("ev2", "sq1", "s2"),
        _ev("ev3", "sq2", "s3"), _ev("ev4", "sq2", "s4"),
    ]
    report = await gc.check("q", _subs(), evidences,
                            domain_of={f"s{i}": f"d{i}.it" for i in range(1, 5)})
    assert report.contradictions
    assert report.complete is False  # kept open for the contradiction


async def test_malformed_llm_json_never_raises(tmp_path):
    fake = FakeLLMClient(["definitely not json"])
    gc = GapChecker(fake, prompts_dir=_tmp_prompts(tmp_path))
    evidences = [_ev("ev1", "sq1", "s1"), _ev("ev2", "sq1", "s2")]

    report = await gc.check("q", _subs(), evidences,
                            domain_of={"s1": "a.it", "s2": "b.it"})
    # malformed verdict -> no exception, coverage still decided in code;
    # sq2 has no evidence so the run is NOT complete (correct behavior)
    assert report.complete is False
    assert report.status_by_id["sq1"] == "coperta"
    assert report.status_by_id["sq2"] == "non_coperta"
    assert report.new_queries == []


def test_compute_coverage_pure():
    subs = [SubQuestion(subquestion_id="a", text="x", idx=1)]
    evs = [_ev("e1", "a", "s1")]
    complete, status = compute_coverage(subs, evs, {"s1": "d1.it"})
    assert status == {"a": "parziale"}
    evs.append(_ev("e2", "a", "s2"))
    complete, status = compute_coverage(subs, evs, {"s1": "d1.it", "s2": "d2.it"})
    assert complete is True and status == {"a": "coperta"}
