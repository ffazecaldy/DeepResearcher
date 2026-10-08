"""P1-5: provenance chain continuity — question -> subquestion -> query ->
result -> source -> chunk -> evidence -> claim -> verification must be
traversable in the persisted graph (not just 'nodes exist')."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.provenance.tracker import ProvenanceTracker


@pytest.mark.asyncio
async def test_provenance_chain_is_continuous(tmp_path, monkeypatch):
    from tests.integration.test_pipeline_mock import _build
    from app.config import Depth
    monkeypatch.chdir(tmp_path)
    orch, storage, settings = _build(tmp_path, monkeypatch)
    outcome = await orch.run("Chi ha brevettato il telefono?", "it",
                             Depth.RAPIDA, "run_prov1", __import__("asyncio").Event())
    assert outcome.status == "completed", storage.get_run("run_prov1")["error"]
    run_id = "run_prov1"

    g = ProvenanceTracker(storage, run_id).export_graph()
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = {(e["from"], e["to"]) for e in g["edges"]}

    # every step of the chain is present and CONNECTED to the next
    subs = [n for n in nodes if "_sq" in n]
    queries = [n for n in nodes if "_q" in n and "_sq" not in n]
    assert subs and queries, "chain must contain subquestions and queries"

    # subquestion -> query edges exist for persisted queries
    qrows = storage._query(
        "SELECT id, subquestion_id FROM queries WHERE run_id=?", (run_id,))
    assert qrows, "queries must be persisted"
    for q in qrows:
        assert (q["subquestion_id"], q["id"]) in edges or \
               any(e[0] == q["subquestion_id"] and e[1] == q["id"] for e in edges), \
               f"query {q['id']} not linked to its subquestion"

    # query -> result rows exist (P1-5: add_search_result called)
    res = storage.search_results_for_run(run_id)
    assert res, "search_results must be persisted"

    # result URL -> source URL: every fetched source was found by >=1 query
    sources = storage.sources_for_run(run_id)
    assert sources
    res_urls = {r["url"] for r in res}
    for s in sources:
        assert s["url"] in res_urls or s["url_final"] in res_urls or \
            any(s["url"].rstrip("/") == u.rstrip("/") for u in res_urls), \
            f"source {s['id']} has no query->result link"

    # source -> chunk -> evidence -> claim: walk the evidence back to a query
    evidences = storage.evidences_for_run(run_id)
    assert evidences
    for e in evidences:
        assert e["id"] and e["source_id"] in {s["id"] for s in sources}, \
            f"evidence {e['id']} references an unknown source"

    claims = storage.claims_for_run(run_id)
    assert claims
    claim_ev = {c["id"]: json.loads(c["evidence_ids_json"] or "[]") for c in claims}
    ev_ids = {e["id"] for e in evidences}
    for cid, eids in claim_ev.items():
        assert eids, f"claim {cid} has no evidence"
        for eid in eids:
            assert eid in ev_ids, f"claim {cid} references unknown evidence {eid}"

    # verification rows reference existing claims
    ver = storage.verifications_for_run(run_id)
    assert ver
    claim_ids = {c["id"] for c in claims}
    for v in ver:
        assert v["claim_id"] in claim_ids, \
            f"verification references unknown claim {v['claim_id']}"


@pytest.mark.asyncio
async def test_planner_model_ids_are_resolved_not_silently_sq1(tmp_path, monkeypatch):
    """A query referencing a VALID model subquestion id resolves to THAT
    subquestion (not sq1) after id normalization (P1-5)."""
    from app.agent.planner import Planner
    from tests.fakes import FakeLLMClient
    plan_json = json.dumps({
        "title": "T",
        "subquestions": [
            {"subquestion_id": "audit_sq1", "text": "Prima sotto?"},
            {"subquestion_id": "audit_sq2", "text": "Seconda sotto?"},
        ],
        "queries": [
            {"subquestion_id": "audit_sq2", "text": "query sulla seconda"},
            {"subquestion_id": "audit_sq1", "text": "query sulla prima"},
        ],
    })
    planner = Planner(FakeLLMClient([plan_json]))
    plan = await planner.make_plan("domanda", "it", max_subquestions=4)
    by_text = {q.text: q.subquestion_id for q in plan.queries}
    # sq2 = seconda sotto: the query about the second subquestion points there
    assert by_text["query sulla seconda"] == "sq2"
    assert by_text["query sulla prima"] == "sq1"
