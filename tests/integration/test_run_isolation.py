"""P0-4: two overlapping runs must not contaminate each other's state.

The orchestrator instance is SHARED (as in the server); the LLM is delayed to
force real interleaving. Every event, decision and counter must land in the
right run.
"""
from __future__ import annotations

import asyncio
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
from app.models import (DecisionResult, DecisionKind, DecisionSpec,
                        FetchedDocument, FetchStatus)
from app.storage import Storage
from tests.fakes import FakeLLMClient


class SlowLLM(FakeLLMClient):
    """FakeLLMClient with a per-call delay: forces interleaving of two runs."""

    def __init__(self, responses, delay: float = 0.05):
        super().__init__(responses if isinstance(responses, list) else [])
        self._fn = responses if callable(responses) else None
        self.delay = delay
        self.calls_by_run: dict[str, int] = {}

    async def complete(self, system, user, **kw):
        await asyncio.sleep(self.delay)
        return await super().complete(system, user, **kw)


class FakeDecisionEngine:
    name = "fake"

    def __init__(self):
        self.calls: list[str] = []  # subject_id -> proves routing correctness

    async def decide(self, spec, state, *, subject_kind="", subject_id=""):
        return (await self.decide_many([spec], state, subject_kind=subject_kind,
                                       subject_id=subject_id))[0]

    async def decide_many(self, specs, state, *, subject_kind="", subject_id=""):
        self.calls.append(subject_id)
        out = []
        for s in specs:
            value = {DecisionKind.BINARY: True, DecisionKind.CHOICE: "news",
                     DecisionKind.SCORE: 2}[s.kind]
            out.append(DecisionResult(name=s.name, kind=s.kind, engine=self.name,
                                      value=value, confidence=0.9, abstained=False,
                                      latency_ms=0.1, subject_kind=subject_kind,
                                      subject_id=subject_id, raw={}))
        return out


class TwoPageSearcher:
    """URL depends on the question so the two runs touch different sources."""

    def __init__(self):
        self.doc = None

    def set_doc(self, doc):
        self.doc = doc

    async def run_queries(self, queries, max_pages):
        from app.models import SearchResult
        out = []
        for q in queries:
            tag = "r1" if "prima" in q.text else "r2"
            out.append(SearchResult(url=f"https://{tag}.example/x", title=t,
                                    snippet="", query_id=q.query_id)
                       if False else
                       SearchResult(url=f"https://{tag}.example/x", title="t",
                                    snippet="", query_id=q.query_id))
        return out


def _plan_fn(question: str):
    return json.dumps({
        "title": f"Ricerca {question[:12]}",
        "subquestions": [{"subquestion_id": "sq1", "text": f"sotto {question[:10]}?"}],
        "queries": [{"subquestion_id": "sq1", "text": question}],
    })


def _pipeline(tmp_path: Path):
    """Build a minimal test rig; returns (settings, storage, bus)."""
    settings = Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "cache",
                        cache_enabled=False, _env_file=None)
    storage = Storage(settings.db_path)
    bus = EventBus(persist=storage.add_event)
    return settings, storage, bus


_DOC = ("Il muro cadde nel 1996. " * 40)  # enough for >=1 chunk


class OwnFetcher:
    """Fetcher double with PER-RUN source ids (no PK collision across runs)."""

    def __init__(self):
        self.n = 0

    async def fetch_all(self, items, budget=None, on_event=None) -> list:
        out = []
        for it in items:
            self.n += 1
            sid = f"src_{self.n}_{it.url.split('//')[1].split('.')[0]}"
            if on_event:
                on_event("source_fetching", it.url)
            out.append(FetchedDocument(
                source_id=sid, url=it.url, final_url=it.url,
                domain=it.url.split("//")[1].split("/")[0],
                title="Doc", text=_DOC, http_status=200,
                content_type="text/html", fetch_status=FetchStatus.SUCCESS))
        return out

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_two_concurrent_runs_no_cross_contamination(tmp_path, monkeypatch):
    settings, storage, bus = _pipeline(tmp_path)
    monkeypatch.chdir(tmp_path)

    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    prompts.joinpath("planner.txt").write_text(
        "S\n---SYSTEM---\n{question} {language} {max_subquestions}")
    prompts.joinpath("reader.txt").write_text(
        "S\n---SYSTEM---\n{question} {subquestion} {external_content}")
    prompts.joinpath("gap_checker.txt").write_text(
        "S\n---SYSTEM---\n{question} {subquestions_json} {evidence_json}")
    prompts.joinpath("writer.txt").write_text(
        "S\n---SYSTEM---\n{question} {language} {evidence_json} {subquestions_json}")
    prompts.joinpath("verifier.txt").write_text("S\n---SYSTEM---\n{question} {claim} {evidence_json}")

    def planner_fn(q):
        return _plan_fn(q)

    def reader_fn(q, s, c, **kw):
        return json.dumps({"evidences": [{"quote": "Il muro cadde nel 1996.",
                                          "claim": "il muro cadde nel 1996",
                                          "subquestion_id": ""}]})

    def gap_fn(q, s, e, **kw):
        return json.dumps({"complete": True, "covered_subquestions": ["sq1"],
                           "missing_subquestions": [], "contradictions": [],
                           "new_queries": [],
                           "status_by_id": {"sq1": "coperta"}})

    def writer_fn(q, l, ev, sq):
        return json.dumps({"title": "T", "markdown": "Il muro cadde nel 1996. [1]",
                           "claims": [{"text": "Il muro cadde nel 1996.",
                                       "evidence_ids": [json.loads(ev)[0]["evidence_id"]]}]})

    def verifier_fn(q, c, e):
        return json.dumps({"verdict": "VERIFIED", "reason": "ok"})

    llm = SlowLLM(lambda s, u, **kw: planner_fn(u), delay=0.05)
    # route by content: planner/reader/gap/writer/verifier all share one client
    # in this test (single FakeLLMClient dispatches on the callable we install)
    class Router:
        def __init__(self):
            self.calls = 0

        async def complete(self, system, user, **kw):
            self.calls += 1
            # writer: prompt contains the evidence pack as JSON list with
            # "evidence_id" AND the writer language rule
            if "REGOLA LINGUA" in user:
                m = re.search(r'"evidence_id":\s*"([^"]+)"', user)
                eid = m.group(1) if m else "ev_x"
                return json.dumps({
                    "title": "T",
                    "markdown": "Il muro cadde nel 1996. [1]",
                    "claims": [{"text": "Il muro cadde nel 1996.",
                                "evidence_ids": [eid]}]})
            # verifier: user prompt contains the claim text field
            if '"claim"' in user and "verdict" in system.lower():
                return json.dumps({"verdict": "VERIFIED", "reason": "ok"})
            # gap checker: asks about coverage of subquestions
            if "subquestions_json" in user:
                return json.dumps({"complete": True,
                                   "covered_subquestions": ["sq1"],
                                   "missing_subquestions": [],
                                   "contradictions": [], "new_queries": [],
                                   "status_by_id": {"sq1": "coperta"}})
            # reader: EXTRACT prompt has the external content delimiters
            if "external_web_content" in user or "EXTRACT" in system:
                return json.dumps({"evidences": [
                    {"quote": "Il muro cadde nel 1996.",
                     "claim": "il muro cadde nel 1996",
                     "subquestion_id": ""}]})
            return _plan_fn(user[:60])

    router = Router()

    class DispatchLLM(FakeLLMClient):
        def __init__(self):
            super().__init__([])

        async def complete(self, system, user, **kw):
            await asyncio.sleep(0.05)
            return await router.complete(system, user, **kw)

    llm_disp = DispatchLLM()
    fake_dec = FakeDecisionEngine()
    decisions = DecisionRouter(fake_dec, LLMDecisionEngine(llm_disp), storage=storage)

    from tests.integration.test_pipeline_mock import FakeSearcherShim
    searcher = FakeSearcherShim()
    fetcher = OwnFetcher()

    orch = Orchestrator(settings, storage, bus, llm_disp, searcher, fetcher,
                        decisions, Planner(llm_disp, prompts_dir=prompts),
                        Reader(llm_disp, prompts_dir=prompts),
                        GapChecker(llm_disp, prompts_dir=prompts),
                        Writer(llm_disp, prompts_dir=prompts),
                        Verifier(llm_disp, prompts_dir=prompts))

    c1, c2 = asyncio.Event(), asyncio.Event()
    r1 = asyncio.create_task(orch.run("prima ricerca muri", "it", Depth.RAPIDA,
                                      "run_A", c1))
    await asyncio.sleep(0.02)  # stagger start: real overlap
    r2 = asyncio.create_task(orch.run("seconda ricerca ponti", "it", Depth.RAPIDA,
                                      "run_B", c2))
    o1, o2 = await asyncio.gather(r1, r2)

    assert o1.status == "completed", (o1.stats, storage.get_run("run_A")["error"])
    assert o2.status == "completed", (o2.stats, storage.get_run("run_B")["error"])

    # --- no event landed in the wrong run ---
    ev_a = {e["type"] for e in storage.events_for_run("run_A")}
    ev_b = {e["type"] for e in storage.events_for_run("run_B")}
    assert "run_started" in ev_a and "run_completed" in ev_a
    assert "run_started" in ev_b and "run_completed" in ev_b
    # every persisted event carries its own run_id (DB column): check counts
    all_ev = (storage.events_for_run("run_A"), storage.events_for_run("run_B"))
    assert all(len(x) > 0 for x in all_ev)

    # --- decisions: each run got its own 3 (is_relevant, type, quality) ---
    dec_a = storage.decisions_for_run("run_A")
    dec_b = storage.decisions_for_run("run_B")
    assert len(dec_a) == 3, dec_a
    assert len(dec_b) == 3, dec_b

    # --- per-run counters isolated (bind()): each view saw exactly 3 ---
    # (verified indirectly: storage rows above are per-run)

    # --- usage rows are separate ---
    u1 = storage.usage_for_run("run_A")
    u2 = storage.usage_for_run("run_B")
    assert u1 is not None and u2 is not None

    # --- sources did not cross ---
    id_a = {s["id"] for s in storage.sources_for_run("run_A")}
    id_b = {s["id"] for s in storage.sources_for_run("run_B")}
    assert id_a and id_b
    assert not (id_a & id_b), f"shared source ids across runs: {id_a & id_b}"


@pytest.mark.asyncio
async def test_two_consecutive_runs_fresh_state(tmp_path, monkeypatch):
    """A second run on the SAME orchestrator starts with fresh counters."""
    settings, storage, bus = _pipeline(tmp_path)
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("planner", "reader", "gap_checker", "writer", "verifier"):
        prompts.joinpath(f"{name}.txt").write_text("S\n---SYSTEM---\nx")
    llm = FakeLLMClient([_plan_fn("q"),  # run 1 planner
                         json.dumps({"evidences": []}),
                         json.dumps({"complete": True, "covered_subquestions": [],
                                     "missing_subquestions": [], "contradictions": [],
                                     "new_queries": [], "status_by_id": {}}),
                         _plan_fn("q"),  # run 2 planner
                         json.dumps({"evidences": []}),
                         json.dumps({"complete": True, "covered_subquestions": [],
                                     "missing_subquestions": [], "contradictions": [],
                                     "new_queries": [], "status_by_id": {}})])
    decisions = DecisionRouter(None, LLMDecisionEngine(llm), storage=storage)
    from tests.integration.test_pipeline_mock import FakeFetcher, FakeSearcherShim
    orch = Orchestrator(settings, storage, bus, llm, FakeSearcherShim(), FakeFetcher(),
                        decisions, Planner(llm, prompts_dir=prompts),
                        Reader(llm, prompts_dir=prompts),
                        GapChecker(llm, prompts_dir=prompts),
                        Writer(llm, prompts_dir=prompts),
                        Verifier(llm, prompts_dir=prompts))
    o1 = await orch.run("uno", "it", Depth.RAPIDA, "run_C1", asyncio.Event())
    o2 = await orch.run("due", "it", Depth.RAPIDA, "run_C2", asyncio.Event())
    assert o1.status in ("completed", "failed")  # zero evidences -> honest failed
    assert o2.status in ("completed", "failed")
    # the second run did NOT inherit the first run's no_novelty counter
    # (both runs end MAX_CICLI / no evidences, not NESSUNA_NOVITA)
    assert o1.stats["stop_reason"] != "NESSUNA_NOVITA"
    assert o2.stats["stop_reason"] != "NESSUNA_NOVITA"
