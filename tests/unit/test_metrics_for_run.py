"""Regressione schema metrics_for_run: copertura sotto-domande (gap status
salvato), claim_support (verdict + prefisso [support:X]), citation_correctness
(marker [n] vs bibliografia Fonti), reading_yield, cycle_contribution.
"""
from __future__ import annotations

from pathlib import Path

from app.models import Chunk, Event, Evidence
from app.storage import Storage


def _seed(storage: Storage) -> str:
    run_id = "run_metrics"
    storage.usage_init(run_id)
    storage.create_run(run_id, "Chi ha brevettato il telefono?", "it", "standard")
    storage.add_subquestion(run_id, "sq1", 1, "Chi ha brevettato?")
    storage.add_subquestion(run_id, "sq2", 2, "Quando è arrivata la radio?")
    storage.add_subquestion(run_id, "sq3", 3, "Dove?")

    # fonti: 2 acquire distinte + 1 duplicato di contenuto + 1 fetch fallito
    storage.add_source("s1", run_id, "https://a.example/1", "", "a.example",
                       "T", 200, "text/html", "SUCCESS", None, None, [], None,
                       100, content_hash="h1")
    storage.add_source("s2", run_id, "https://b.example/1", "", "b.example",
                       "T", 200, "text/html", "SUCCESS", None, None, [], None,
                       100, content_hash="h2")
    storage.add_source("s2dup", run_id, "https://b.example/copy", "",
                       "b.example", "T", 200, "text/html", "SUCCESS", None,
                       None, [], None, 100, content_hash="h2")
    storage.add_source("s4", run_id, "https://c.example/1", "", "c.example",
                       "T", None, "", "FETCH_FAILED", None, None, [],
                       "http 500", 0)
    storage.update_source_evaluation("s1", "news", 2, 0.9, True)
    storage.update_source_evaluation("s2", "news", 2, 0.9, True)

    # chunk + evidenze da 2 fonti distinte
    storage.add_chunk(Chunk(chunk_id="c1", source_id="s1", idx=0,
                            text="testo uno", char_start=0, char_end=10))
    storage.add_chunk(Chunk(chunk_id="c2", source_id="s2", idx=0,
                            text="testo due", char_start=0, char_end=10))
    for i, (eid, sid, cid, sq) in enumerate([
            ("ev1", "s1", "c1", "sq1"), ("ev2", "s1", "c1", "sq1"),
            ("ev3", "s2", "c2", "sq2")]):
        storage.add_evidence(Evidence(evidence_id=eid, run_id=run_id,
                                      source_id=sid, chunk_id=cid,
                                      quote=f"q{i}", claim=f"claim {i}",
                                      subquestion_id=sq))

    # verifications: 3 con prefisso [support:X], 1 senza (not_conclusive)
    storage.add_verification(run_id, "clm1", "VERIFIED",
                             "[support:supported] quote ok", None)
    storage.add_verification(run_id, "clm2", "WEAKENED",
                             "[support:contradicted] evidenza contraddice", None)
    storage.add_verification(run_id, "clm3", "VERIFIED",
                             "[support:insufficient] una sola fonte", None)
    storage.add_verification(run_id, "clm4", "FAILED",
                             "verification error: boom", None)

    # markdown salvato: 4 marker inline, 3 con destinazione in Fonti
    storage.save_report(run_id, "rep_metrics", "Titolo",
                        "# Titolo\n\nFatto uno [1] e due di nuovo [1] e "
                        "tre [2].\n\nFrase senza destinazione [9].\n\n"
                        "## Fonti\n\n1. Fonte A — https://a.example/1 "
                        "(a.example)\n2. Fonte B — https://b.example/1 "
                        "(b.example)\n")

    # eventi: registri di ciclo (l'ultimo gap_status vince) + gap_detected
    # vecchio che NON deve vincere + contributi per ciclo
    storage.add_event(Event(type="cycle_registry", run_id=run_id, cycle=1,
                            payload={"cycle": 1, "gap_status": {
                                "sq1": "non_coperta", "sq2": "non_coperta",
                                "sq3": "non_coperta"}}))
    storage.add_event(Event(type="gap_detected", run_id=run_id, cycle=1,
                            payload={"status": {"sq1": "non_coperta"}}))
    storage.add_event(Event(type="cycle_registry", run_id=run_id, cycle=2,
                            payload={"cycle": 2, "gap_status": {
                                "sq1": "coperta", "sq2": "parziale",
                                "sq3": "non_coperta"}}))
    storage.add_event(Event(type="cycle_contribution", run_id=run_id, cycle=1,
                            payload={"new_evidences": 2, "new_domains": 1}))
    storage.add_event(Event(type="cycle_contribution", run_id=run_id, cycle=2,
                            payload={"new_evidences": 1, "new_domains": 0}))
    return run_id


def test_metrics_for_run_schema_and_values(tmp_path):
    storage = Storage(tmp_path / "t.db")
    run_id = _seed(storage)

    m = storage.metrics_for_run(run_id)

    # ---- schema ----
    assert set(m.keys()) == {"run_id", "subquestion_coverage", "claim_support",
                             "citation_correctness", "reading_yield",
                             "cycle_contribution"}
    assert m["run_id"] == run_id
    assert set(m["subquestion_coverage"].keys()) == {
        "plan_total", "covered", "partial", "unresolved", "covered_of",
        "covered_ratio", "source"}
    assert set(m["claim_support"].keys()) == {
        "verifiable", "supported", "contradicted", "insufficient", "mixed",
        "not_conclusive", "verdicts"}
    assert set(m["citation_correctness"].keys()) == {
        "inline_markers", "with_target", "without_target"}
    assert set(m["reading_yield"].keys()) == {
        "acquired", "of_fetch_attempts", "readable", "of_acquired",
        "produced_evidence", "of_readable", "produced_ratio"}

    # ---- subquestion_coverage: ultimo gap status salvato (cycle 2) ----
    cov = m["subquestion_coverage"]
    assert cov["plan_total"] == 3
    assert cov["covered"] == 1 and cov["partial"] == 1 and cov["unresolved"] == 1
    assert cov["covered_of"] == 3
    assert cov["covered_ratio"] == round(1 / 3, 3)
    assert cov["source"] == "cycle_registry.gap_status"  # non gap_detected

    # ---- claim_support ----
    cs = m["claim_support"]
    assert cs == {"verifiable": 4, "supported": 1, "contradicted": 1,
                  "insufficient": 1, "mixed": 0, "not_conclusive": 1,
                  "verdicts": {"VERIFIED": 2, "WEAKENED": 1, "FAILED": 1}}

    # ---- citation_correctness ----
    cc = m["citation_correctness"]
    assert cc == {"inline_markers": 4, "with_target": 3, "without_target": 1}

    # ---- reading_yield (numeratore/denominatore espliciti) ----
    ry = m["reading_yield"]
    assert ry["acquired"] == 2            # 3 SUCCESS ma s2dup = stesso hash
    assert ry["of_fetch_attempts"] == 4   # 3 SUCCESS + 1 FETCH_FAILED
    assert ry["readable"] == 2            # accepted=1 e fetch ok
    assert ry["of_acquired"] == 2
    assert ry["produced_evidence"] == 2   # s1 + s2 hanno evidenze
    assert ry["of_readable"] == 2
    assert ry["produced_ratio"] == 1.0

    # ---- cycle_contribution ----
    assert m["cycle_contribution"] == [
        {"cycle": 1, "new_evidences": 2, "new_domains": 1},
        {"cycle": 2, "new_evidences": 1, "new_domains": 0},
    ]


def test_metrics_for_run_falls_back_to_gap_detected(tmp_path):
    """Run senza cycle_registry (pre-registro): il gap status arriva da
    gap_detected.status."""
    storage = Storage(tmp_path / "t.db")
    run_id = "run_old"
    storage.create_run(run_id, "domanda", "it", "standard")
    storage.add_subquestion(run_id, "sq1", 1, "Chi?")
    storage.add_subquestion(run_id, "sq2", 2, "Quando?")
    storage.add_event(Event(type="gap_detected", run_id=run_id, cycle=1,
                            payload={"status": {"sq1": "coperta",
                                                "sq2": "parziale"}}))

    m = storage.metrics_for_run(run_id)
    cov = m["subquestion_coverage"]
    assert cov["source"] == "gap_detected.status"
    assert cov["plan_total"] == 2
    assert cov["covered"] == 1 and cov["partial"] == 1 and cov["unresolved"] == 0


def test_metrics_for_run_empty_run_zeroed_schema(tmp_path):
    """Run sconosciuto: struttura completa ma tutti zero, senza eccezioni."""
    storage = Storage(tmp_path / "t.db")
    m = storage.metrics_for_run("missing")
    assert m["run_id"] == "missing"
    assert m["subquestion_coverage"] == {
        "plan_total": 0, "covered": 0, "partial": 0, "unresolved": 0,
        "covered_of": 0, "covered_ratio": None, "source": ""}
    assert m["claim_support"]["verifiable"] == 0
    assert m["claim_support"]["not_conclusive"] == 0
    assert m["claim_support"]["verdicts"] == {}
    assert m["citation_correctness"] == {"inline_markers": 0, "with_target": 0,
                                         "without_target": 0}
    assert m["reading_yield"] == {
        "acquired": 0, "of_fetch_attempts": 0, "readable": 0, "of_acquired": 0,
        "produced_evidence": 0, "of_readable": 0, "produced_ratio": None}
    assert m["cycle_contribution"] == []
