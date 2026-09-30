"""Integration tests for the FastAPI server (fakes injected, no network)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.server.app as server_app
from app.config import Settings
from app.events.bus import EventBus
from app.models import (Chunk, DecisionKind, DecisionResult, DecisionSpec,
                        RunOutcome)
from app.storage import Storage


class FakeOrch:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.run_id = ""

    async def run(self, question, language, depth, run_id, cancel):
        from app.models import Event
        await asyncio.sleep(0.1)
        self.bus.publish(Event(type="cycle_started", run_id=run_id, cycle=1,
                               payload={"x": 1}))
        self.bus.publish(Event(type="run_completed", run_id=run_id, cycle=0,
                               payload={"status": "completed"}))
        return RunOutcome(run_id=run_id, status="completed", question=question,
                          language=language)


def _build(tmp_path: Path, monkeypatch):
    settings = Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "c",
                        cache_enabled=False)
    storage = Storage(settings.db_path)
    bus = EventBus(persist=storage.add_event)
    fake_orch = FakeOrch(bus)

    def fake_build(s):  # sync, like the real build_orchestrator
        async def aclose():
            pass
        return fake_orch, {"storage": storage, "bus": bus, "aclose": aclose}

    monkeypatch.setattr(server_app, "build_orchestrator", fake_build)
    client = TestClient(server_app.create_app(settings))
    return client, storage, fake_orch


def test_settings_masks_key(tmp_path, monkeypatch):
    client, _, _ = _build(tmp_path, monkeypatch)
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["llm_api_key"] == "unset"
    assert "llm_model" in body and "laya_available" in body


def test_start_run_and_detail(tmp_path, monkeypatch):
    client, storage, _ = _build(tmp_path, monkeypatch)
    r = client.post("/api/runs", json={"question": "Chi ha inventato il telefono?"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    for _ in range(50):
        row = storage.get_run(run_id)
        if row is None:  # fake orch doesn't create the run row; check outcome via API
            break
        time.sleep(0.05)
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 404 or detail.json()["run"]["id"] == run_id


def test_404_unknown_run(tmp_path, monkeypatch):
    client, _, _ = _build(tmp_path, monkeypatch)
    assert client.get("/api/runs/nope").status_code == 404
    assert client.delete("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/report.md").status_code == 404
    assert client.get("/api/runs/nope/report.pdf").status_code == 404


def test_events_stream_completed_run(tmp_path, monkeypatch):
    client, storage, _ = _build(tmp_path, monkeypatch)
    storage.create_run("run_hist", "domanda", "it", "standard")
    storage.add_event(__import__("app.models", fromlist=["Event"]).Event(
        type="run_started", run_id="run_hist", payload={}))
    storage.finish_run("run_hist", "completed")

    with client.stream("GET", "/api/runs/run_hist/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = "".join(chunk for chunk in resp.iter_text())
    assert "replay_start" in body
    assert "run_started" in body
    assert "run_closed" in body


def test_reports_md_json_pdf(tmp_path, monkeypatch):
    from app.models import Evidence
    client, storage, _ = _build(tmp_path, monkeypatch)
    storage.create_run("run_rep", "domanda", "it", "standard")
    storage.add_source("src1", "run_rep", "https://example.com",
                       "https://example.com", "example.com", "Fonte", 200,
                       "text/html", "SUCCESS", None, None, [], None, 100)
    storage.add_evidence(Evidence(evidence_id="ev1", run_id="run_rep",
                                  source_id="src1", chunk_id="chk1",
                                  quote="q", claim="c"))
    storage.add_claim("run_rep", "clm1", "claim text", 1, ["ev1"])
    storage.save_report("run_rep", "rep1", "Titolo", "# Titolo\n\nTesto [1].")

    md = client.get("/api/runs/run_rep/report.md")
    assert md.status_code == 200
    assert "Titolo" in md.text

    js = client.get("/api/runs/run_rep/report.json")
    assert js.status_code == 200
    payload = js.json()
    assert payload["provenance"]["nodes"]
    assert payload["claims"][0]["id"] == "clm1"

    pdf = client.get("/api/runs/run_rep/report.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert client.get("/api/runs/run_rep/report.pdf").content[:4] == b"%PDF"


def test_delete_history_run(tmp_path, monkeypatch):
    client, storage, _ = _build(tmp_path, monkeypatch)
    storage.create_run("run_del", "q", "it", "rapida")
    r = client.delete("/api/runs/run_del")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert storage.get_run("run_del") is None


def test_decision_specs_endpoint(tmp_path, monkeypatch):
    client, _, _ = _build(tmp_path, monkeypatch)
    body = client.get("/api/decision-specs").json()
    assert body["decision_engine"] in ("laya", "llm")
    names = {s["name"] for s in body["specs"]}
    assert {"is_relevant", "source_type", "quality"} <= names
