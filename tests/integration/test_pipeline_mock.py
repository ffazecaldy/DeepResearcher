"""End-to-end pipeline test with fakes for LLM, search and decisions.

The fetcher is a fake returning pre-built documents (no network); everything
else runs for real against a tmp SQLite database.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.agent.gap_checker import GapChecker
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.agent.reader import Reader
from app.agent.verifier import Verifier
from app.agent.writer import Writer
from app.config import Depth, Settings
from app.decision.base import DecisionRouter
from app.decision.llm_fallback import LLMDecisionEngine
from app.events.bus import EventBus
from app.limits import DiskCache
from app.models import (DecisionKind, DecisionResult, DecisionSpec,
                        FetchedDocument, FetchStatus, SearchResultItem)
from app.storage import Storage
from tests.fakes import FakeLLMClient

DOC_TEXT = (
    "Storia delle telecomunicazioni. "
    "Bell brevettò il telefono nel 1876 dopo anni di esperimenti. "
    " " * 120  # push over the 200-char SUCCESS threshold
)

PLAN_JSON = {
    "title": "Storia del telefono",
    "language": "it",
    "subquestions": [{"subquestion_id": "x1", "text": "Chi ha brevettato?"}],
    "queries": [{"subquestion_id": "x1", "text": "invenzione telefono brevetto"}],
}
READER_JSON = {"evidences": [
    {"claim": "Bell brevettò il telefono nel 1876",
     "quote": "Bell brevettò il telefono nel 1876"},
    {"claim": "Gli esperimenti durarono anni",
     "quote": "dopo anni di esperimenti"},
]}
VERIFIER_JSON = {"verdict": "VERIFIED", "reason": "quote matches"}


class FakeSearchClient:
    name = "fake"

    async def search(self, query: str, max_results: int) -> list[SearchResultItem]:
        return [SearchResultItem(query_id="", url="https://example.com/bell",
                                 title="Bell story", position=0)]

    async def aclose(self) -> None:
        pass


class FakeFetcher:
    async def fetch_all(self, items, budget=None, on_event=None) -> list[FetchedDocument]:
        if on_event:
            on_event("source_fetching", "https://example.com/bell")
        return [FetchedDocument(
            source_id="src1", url="https://example.com/bell",
            final_url="https://example.com/bell", domain="example.com",
            title="Bell story", text=DOC_TEXT, http_status=200,
            content_type="text/html", fetch_status=FetchStatus.SUCCESS)]

    async def aclose(self) -> None:
        pass


class FakeDecisionEngine:
    name = "fake"

    async def decide(self, spec, state, *, subject_kind="", subject_id=""):
        return (await self.decide_many([spec], state, subject_kind=subject_kind,
                                       subject_id=subject_id))[0]

    async def decide_many(self, specs, state, *, subject_kind="", subject_id=""):
        out = []
        for s in specs:
            value = {DecisionKind.BINARY: True, DecisionKind.CHOICE: "news",
                     DecisionKind.SCORE: 2}[s.kind]
            out.append(DecisionResult(name=s.name, kind=s.kind, engine=self.name,
                                      value=value, confidence=0.9,
                                      subject_kind=subject_kind,
                                      subject_id=subject_id))
        return out


def _writer_fn(system, user, force_json):
    eids = re.findall(r'"evidence_id":\s*"(ev_[a-z0-9]+)"', user)
    return json.dumps({
        "title": "Storia del telefono",
        "markdown": "Bell brevettò il telefono nel 1876. Gli esperimenti durarono anni.",
        "claims": [{"text": "Bell brevettò il telefono nel 1876.",
                    "evidence_ids": [eids[0]]},
                   {"text": "Gli esperimenti durarono anni.",
                    "evidence_ids": eids[1:]}],
    })


def _build(tmp_path: Path, monkeypatch=None):
    settings = Settings(db_path=tmp_path / "t.db",
                        cache_dir=tmp_path / "cache", cache_enabled=False,
                        _env_file=None)  # isolate from the real .env
    storage = Storage(settings.db_path)
    bus = EventBus(persist=storage.add_event)
    monkeypatcher = monkeypatch

    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    templates = {
        "planner.txt": "S\n---SYSTEM---\n{question} {language} {max_subquestions}",
        "reader.txt": "S\n---SYSTEM---\n{question} {subquestion} {external_content}",
        "gap_checker.txt": "S\n---SYSTEM---\n{question} {subquestions_json} {evidence_json}",
        "writer.txt": "S\n---SYSTEM---\n{question} {language} {evidence_json} {subquestions_json}",
        "verifier.txt": "S\n---SYSTEM---\n{question} {claim} {evidence_json}",
    }
    for name, body in templates.items():
        (prompts / name).write_text(body, encoding="utf-8")

    llm_plan = FakeLLMClient([json.dumps(PLAN_JSON)])
    llm_read = FakeLLMClient([json.dumps(READER_JSON)])
    llm_gap = FakeLLMClient([])
    llm_write = FakeLLMClient(_writer_fn)
    llm_ver = FakeLLMClient([json.dumps(VERIFIER_JSON)])

    decisions = DecisionRouter(FakeDecisionEngine(),
                               LLMDecisionEngine(FakeLLMClient([])),
                               storage=storage)
    orch = Orchestrator(
        settings, storage, bus, llm_plan, FakeSearcherShim(), FakeFetcher(),
        decisions, Planner(llm_plan, prompts_dir=prompts),
        Reader(llm_read, prompts_dir=prompts),
        GapChecker(llm_gap, prompts_dir=prompts),
        Writer(llm_write, prompts_dir=prompts),
        Verifier(llm_ver, prompts_dir=prompts))
    return orch, storage, settings


class FakeSearcherShim:
    """Searcher double: searcher semantics already covered by unit tests."""

    async def run_queries(self, queries, max_pages_per_query):
        return [SearchResultItem(query_id=queries[0].query_id,
                                 url="https://example.com/bell",
                                 title="Bell story", position=0)]


async def test_full_pipeline_mock(tmp_path, monkeypatch):
    orch, storage, settings = _build(tmp_path, monkeypatch)
    run_id = "run_test1"

    outcome = await orch.run("Chi ha brevettato il telefono?", "it",
                             Depth.RAPIDA, run_id, __import__("asyncio").Event())

    assert outcome.status == "completed"
    assert outcome.markdown_path and Path(outcome.markdown_path).exists()
    md = Path(outcome.markdown_path).read_text(encoding="utf-8")
    assert "## Fonti" in md
    assert "example.com" in md
    assert "1876" in md

    assert len(storage.claims_for_run(run_id)) == 2
    assert len(storage.evidences_for_run(run_id)) == 2
    assert len(storage.decisions_for_run(run_id)) == 3  # is_relevant+type+quality
    assert len(storage.sources_for_run(run_id)) == 1

    ev_types = {e["type"] for e in storage.events_for_run(run_id)}
    for expected in ("run_started", "cycle_started", "plan_generated",
                     "search_result_found", "source_fetched", "source_evaluated",
                     "source_reading", "evidence_extracted", "report_chunk",
                     "claim_verified", "run_completed"):
        assert expected in ev_types, expected

    assert outcome.json_path and Path(outcome.json_path).exists()
    payload = json.loads(Path(outcome.json_path).read_text(encoding="utf-8"))
    assert payload["provenance"]["nodes"]
    assert outcome.stats["decisions"].get("fake") == 3


async def test_cancel_before_run(tmp_path, monkeypatch):
    import asyncio

    orch, storage, _ = _build(tmp_path, monkeypatch)
    cancel = asyncio.Event()
    cancel.set()
    outcome = await orch.run("domanda", "it", Depth.STANDARD, "run_test2", cancel)
    assert outcome.status == "cancelled"
    assert storage.get_run("run_test2")["status"] == "cancelled"
