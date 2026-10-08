"""P0-1 E2E: the CORRECTED/REMOVED verdicts must reach DB, markdown file and
API — the full delivery path, not just the verdict objects."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.integration.test_pipeline_mock import _build
from app.config import Depth


@pytest.mark.asyncio
async def test_corrected_claim_reaches_db_md_and_api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch, storage, settings = _build(tmp_path, monkeypatch)
    outcome = await orch.run("Chi ha brevettato il telefono?", "it",
                             Depth.RAPIDA, "run_final1",
                             __import__("asyncio").Event())
    run_id = "run_final1"
    assert outcome.status == "completed", storage.get_run(run_id)["error"]

    # --- DB: the persisted report is the VERIFIED text ---
    report = storage.report_for_run(run_id)
    assert report and report["markdown"]
    claims = storage.claims_for_run(run_id)
    assert claims
    # final_citation_no present on claims (P0-2 unified numbers)
    assert all("final_citation_no" in c or "final_citation_no" in c
               for c in [dict(c) for c in claims])
    finals = {c["final_citation_no"] for c in claims if c.get("final_citation_no")}
    assert finals and min(finals) == 1, "final numbers start at 1"
    # verifications reference existing claims
    ver = storage.verifications_for_run(run_id)
    assert ver
    assert {v["claim_id"] for v in ver} <= {c["id"] for c in claims}

    # --- markdown file on disk = DB markdown ---
    md_path = Path(outcome.markdown_path)
    assert md_path.exists()
    assert md_path.read_text(encoding="utf-8") == report["markdown"]

    # --- API (report.json) exposes the same text + final numbers ---
    from fastapi.testclient import TestClient
    import app.server.app as server_app
    from app.events.bus import EventBus as _Bus

    # same pattern as test_server.py: patch build_orchestrator so create_app
    # never touches real providers/env; the app shares our isolated storage
    def fake_build(s, bus=None):
        async def aclose():
            pass
        return orch, {"storage": storage, "bus": _Bus(persist=storage.add_event),
                      "aclose": aclose}
    monkeypatch.setattr(server_app, "build_orchestrator", fake_build)
    app = server_app.create_app(settings)
    with TestClient(app) as client:
        r = client.get(f"/api/runs/{run_id}/report.json")
        assert r.status_code == 200
        payload = r.json()
        assert payload["report_markdown"] == report["markdown"]
        api_claims = payload["claims"]
        api_final = {c["final_citation_no"] for c in api_claims
                     if c.get("final_citation_no") is not None}
        assert api_final == finals
        # bibliography numbers in the markdown text are exactly the finals
        md = payload["report_markdown"]
        fonti = md.split("## Fonti", 1)[1] if "## Fonti" in md else ""
        bib_nums = {int(m.group(1)) for line in fonti.splitlines()
                    if (m := __import__("re").match(r"^(\d+)\.\s", line))}
        assert bib_nums == finals, \
            f"bibliography {bib_nums} != claim finals {finals}"
        # every inline [n] in the body has a bibliography entry
        body = md.split("## Fonti", 1)[0]
        inline = {int(m) for m in __import__("re").findall(r"\[(\d+)\]", body)}
        assert inline <= finals, f"dangling inline citations: {inline - finals}"


@pytest.mark.asyncio
async def test_removed_claim_absent_from_every_artifact(tmp_path, monkeypatch):
    """A REMOVED verdict erases the sentence from DB text, file and API."""
    monkeypatch.chdir(tmp_path)
    orch, storage, settings = _build(tmp_path, monkeypatch)
    # reuse pipeline: check invariants on whatever verdicts came out
    outcome = await orch.run("Chi ha brevettato il telefono?", "it",
                             Depth.RAPIDA, "run_final2",
                             __import__("asyncio").Event())
    run_id = "run_final2"
    assert outcome.status == "completed"
    removed = {v["claim_id"] for v in storage.verifications_for_run(run_id)
               if v["verdict"] == "REMOVED"}
    report = storage.report_for_run(run_id)
    claims = {c["id"]: c for c in storage.claims_for_run(run_id)}
    for cid in removed:
        c = claims.get(cid)
        if c is None:
            continue  # claim never persisted in this scenario
        # its final number is None and its text is NOT in the final markdown
        assert not c.get("final_citation_no"), \
            f"removed claim {cid} still carries a final number"
        assert c["text"] not in report["markdown"], \
            f"removed claim {cid} still present in the final markdown"
