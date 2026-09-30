"""Tests for the provenance graph over a populated storage."""
from __future__ import annotations

import os

from app.models import Chunk, DecisionKind, DecisionResult, DecisionSpec, Evidence
from app.provenance.tracker import ProvenanceTracker
from app.storage import Storage


def _populated_storage(tmp_path) -> Storage:
    s = Storage(os.path.join(tmp_path, "p.db"))
    s.create_run("r1", "Domanda di prova?", "it", "standard")
    s.add_subquestion("r1", "sq1", 1, " sotto-domanda ")
    s.add_query("r1", "q1", "sq1", "query text", 1, "tavily")
    s.set_query_status("q1", "done")
    s.add_search_result("r1", "q1", "https://example.com/a?utm_source=x",
                        "https://example.com/a", "Title A", 1)
    s.add_source("src1", "r1", "https://example.com/a", "https://example.com/a",
                 "example.com", "Title A", 200, "text/html", "SUCCESS",
                 "2026-01-01", None, [], None, 1234)
    s.add_chunk(Chunk(chunk_id="chk1", source_id="src1", idx=0,
                      text="full text", char_start=0, char_end=9))
    s.add_evidence(Evidence(evidence_id="ev1", run_id="r1", source_id="src1",
                            chunk_id="chk1", quote="full text",
                            span_start=0, span_end=9, claim="a claim",
                            subquestion_id="sq1"))
    s.add_claim("r1", "clm1", "a claim", 1, ["ev1"])
    s.add_verification("r1", "clm1", "VERIFIED", "ok", None)
    s.add_decision("r1", "is_relevant",
                   DecisionResult(name="is_relevant", kind=DecisionKind.BINARY,
                                  engine="laya", value=True, confidence=0.9,
                                  subject_kind="source", subject_id="src1"))
    return s


def test_full_chain_graph(tmp_path):
    s = _populated_storage(tmp_path)
    g = ProvenanceTracker(s, "r1").export_graph()
    ids = {n["id"] for n in g["nodes"]}
    kinds = {n["id"]: n["kind"] for n in g["nodes"]}
    for expected, kind in [
        ("r1", "run"), ("sq1", "subquestion"), ("q1", "query"), ("src1", "source"),
        ("chk1", "chunk"), ("ev1", "evidence"), ("clm1", "claim"),
    ]:
        assert expected in ids and kinds[expected] == kind, (expected, kinds)
    assert any(n["kind"] == "verification" for n in g["nodes"])
    assert any(n["kind"] == "decision" for n in g["nodes"])
    assert any(n["kind"] == "search_result" for n in g["nodes"])

    pairs = {(e["from"], e["to"]) for e in g["edges"]}
    sr_id = next(n["id"] for n in g["nodes"] if n["kind"] == "search_result")
    chain = [("r1", "sq1"), ("sq1", "q1"), ("q1", sr_id), (sr_id, "src1"),
             ("src1", "chk1"), ("chk1", "ev1"), ("ev1", "clm1")]
    for a, b in chain:
        assert (a, b) in pairs, (a, b, pairs)
    # decision linked to its subject (source)
    assert any(e["from"].startswith("dec:") and e["to"] == "src1" for e in g["edges"])


def test_partial_run_graph(tmp_path):
    s = Storage(os.path.join(tmp_path, "q.db"))
    s.create_run("r2", "sola domanda", "it", "rapida")
    g = ProvenanceTracker(s, "r2").export_graph()
    assert [n["kind"] for n in g["nodes"]] == ["run"]
    assert g["edges"] == []


def test_export_json(tmp_path):
    s = _populated_storage(tmp_path)
    out = ProvenanceTracker(s, "r1").export_json()
    assert '"claim"' in out and "clm1" in out
