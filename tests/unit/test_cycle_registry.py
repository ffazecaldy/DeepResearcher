"""Regressione registro di ciclo (cycle_registry): conteggi corretti per
query/risultati/candidati/acquisti/duplicati/errori + diagnosi del limite
pagine + persistenza in runs.limits_json + budget a gruppi in _read_docs.

Offline: fakes per LLM/search/fetcher, zero rete, zero API key.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.agent.fetcher import content_fingerprint
from app.agent.gap_checker import GapChecker
from app.agent.orchestrator import Orchestrator, RunContext
from app.agent.planner import Planner
from app.agent.reader import Reader
from app.agent.verifier import Verifier
from app.agent.writer import Writer
from app.config import Depth, Settings
from app.decision.base import DecisionRouter
from app.decision.llm_fallback import LLMDecisionEngine
from app.events.bus import EventBus
from app.models import (DecisionKind, DecisionResult, DecisionSpec,
                        FetchedDocument, FetchStatus, SearchResultItem,
                        SubQuestion)
from app.storage import Storage
from tests.fakes import FakeLLMClient

T_BELL = ("Storia delle telecomunicazioni. "
          "Bell brevettò il telefono nel 1876 dopo anni di esperimenti. "
          + " " * 120)  # ~213 char -> SUCCESS, un chunk unico (max_chars=4000)
T_RADIO = ("Storia della radio. "
           "La radio arrivò nelle case italiane negli anni venti del "
           "novecento. " * 3)  # > 200 char -> SUCCESS

PLAN_JSON = {
    "title": "Storia del telefono",
    "language": "it",
    "subquestions": [
        {"subquestion_id": "x1", "text": "Chi ha brevettato il telefono?"},
        {"subquestion_id": "x2", "text": "Quando è arrivata la radio?"},
    ],
    "queries": [{"subquestion_id": "x1", "text": "invenzione telefono brevetto"}],
}
BELL_JSON = {"evidences": [
    {"claim": "Bell brevettò il telefono nel 1876",
     "quote": "Bell brevettò il telefono nel 1876"},
    {"claim": "Gli esperimenti durarono anni",
     "quote": "dopo anni di esperimenti"},
]}
RADIO_JSON = {"evidences": [
    {"claim": "La radio arrivò negli anni venti",
     "quote": "La radio arrivò nelle case italiane negli anni venti"},
]}


def _reader_fn(system, user, force_json):
    """Reader scriptato: risponde in base al contenuto del chunk nel prompt."""
    if "radio" in user:
        return json.dumps(RADIO_JSON)
    return json.dumps(BELL_JSON)


def _querygen_fn(system, user, force_json):
    """QueryGenerator scriptato: una query nuova per la prima sq aperta."""
    m = re.search(r'"subquestion_id"\s*:\s*"([^"]+)"', user)
    sid = m.group(1) if m else "sq1"
    return json.dumps({"queries": [
        {"subquestion_id": sid, "text": "radio novecento evoluzione broadcasting"}]})


def _gap_fn(system, user, force_json):
    return json.dumps({"new_queries": [], "contradictions": []})


def _writer_fn(system, user, force_json):
    eids = re.findall(r'"evidence_id":\s*"(ev_[a-z0-9]+)"', user)
    return json.dumps({
        "title": "Storia del telefono",
        "markdown": "Bell brevettò il telefono nel 1876. "
                    "Gli esperimenti durarono anni.",
        "claims": [{"text": "Bell brevettò il telefono nel 1876.",
                    "evidence_ids": [eids[0]]},
                   {"text": "Gli esperimenti durarono anni.",
                    "evidence_ids": eids[1:]}],
    })


def _verifier_fn(system, user, force_json):
    return json.dumps({"verdict": "VERIFIED", "support": "supported",
                       "reason": "quote matches"})


class ScriptedSearcher:
    """Risultati pre-scriptati per ciclo (url list)."""

    def __init__(self, per_cycle: list[list[str]]):
        self.per_cycle = per_cycle
        self.calls = 0

    async def run_queries(self, queries, max_pages_per_query):
        urls = self.per_cycle[min(self.calls, len(self.per_cycle) - 1)]
        self.calls += 1
        out = []
        for i, url in enumerate(urls):
            qid = queries[0].query_id if queries else "q"
            out.append(SearchResultItem(query_id=qid, url=url,
                                        title=f"risultato {i}", position=i))
        return out


class ScriptedFetcher:
    """Fetch senza rete: url -> documento pre-costruito (hash incluso)."""

    def __init__(self, specs: dict[str, dict]):
        self.specs = specs

    async def fetch_all(self, items, budget=None, on_event=None):
        docs = []
        for it in items:
            spec = self.specs.get(it.url)
            if spec is None:
                docs.append(FetchedDocument(url=it.url,
                                            fetch_status=FetchStatus.FETCH_FAILED,
                                            error="http 500"))
                continue
            docs.append(FetchedDocument(
                source_id=spec["source_id"], url=it.url, final_url=it.url,
                domain=spec["domain"], title=spec.get("title", ""), text=spec["text"],
                http_status=200, content_type="text/html",
                fetch_status=spec.get("status", FetchStatus.SUCCESS),
                content_hash=content_fingerprint(spec["text"]),
                error=spec.get("error")))
        return docs

    async def aclose(self):
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


SPECS = {
    "https://example.com/bell": {"source_id": "src_bell", "domain": "example.com",
                                 "text": T_BELL, "title": "Bell story"},
    "https://example.com/down": {"source_id": "src_down", "domain": "example.com",
                                 "status": FetchStatus.FETCH_FAILED,
                                 "error": "http 500", "text": ""},
    "https://mirror.example/bell": {"source_id": "src_mirror",
                                    "domain": "mirror.example",
                                    "text": T_BELL, "title": "Bell copy"},
    "https://example.com/extra": {"source_id": "src_extra", "domain": "example.com",
                                  "text": T_RADIO, "title": "Radio story"},
}


def _build(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "cache",
                        cache_enabled=False, usage_emit_every_s=0,
                        max_cycles=2, _env_file=None)  # STANDARD -> esattamente 2 cicli
    storage = Storage(settings.db_path)
    bus = EventBus(persist=storage.add_event)
    decisions = DecisionRouter(FakeDecisionEngine(),
                               LLMDecisionEngine(FakeLLMClient([])),
                               storage=storage)
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
    orch = Orchestrator(
        settings, storage, bus, FakeLLMClient(_querygen_fn),
        ScriptedSearcher([
            ["https://example.com/bell", "https://example.com/down"],
            ["https://mirror.example/bell", "https://example.com/extra"],
        ]),
        ScriptedFetcher(SPECS), decisions,
        Planner(FakeLLMClient([json.dumps(PLAN_JSON)]), prompts_dir=prompts),
        Reader(FakeLLMClient(_reader_fn), prompts_dir=prompts),
        GapChecker(FakeLLMClient(_gap_fn), prompts_dir=prompts),
        Writer(FakeLLMClient(_writer_fn), prompts_dir=prompts),
        Verifier(FakeLLMClient(_verifier_fn), prompts_dir=prompts))
    return orch, storage


def _registry_events(storage, run_id) -> list[dict]:
    return [{"cycle": e["cycle"], **json.loads(e["payload_json"])}
            for e in storage.events_for_run(run_id)
            if e["type"] == "cycle_registry"]


async def test_cycle_registry_counts_over_two_cycles(tmp_path):
    """Ciclo 1: 2 risultati, 1 errore fetch, 1 acquisto. Ciclo 2: 1 duplicato
    di contenuto + 1 acquisto; conteggi, new_vs_previous, pages_limit_origin,
    gap_status e persistenza in runs.limits_json."""
    orch, storage = _build(tmp_path)
    run_id = "run_registry"

    outcome = await orch.run("Chi ha brevettato il telefono?", "it",
                             Depth.STANDARD, run_id,
                             __import__("asyncio").Event())
    assert outcome.status == "completed"

    regs = _registry_events(storage, run_id)
    assert len(regs) == 2
    r1, r2 = regs

    # ---- ciclo 1 ----
    assert r1["cycle"] == 1 and r1["role"] == "esplorazione"
    assert r1["queries_executed"] == ["invenzione telefono brevetto"]
    assert r1["results_found"] == 2          # bell + down (nessun filtro)
    assert r1["candidates"] == 2             # entrambi nuovi -> fetch
    assert r1["acquired_new"] == 1           # solo bell (down = errore fetch)
    assert r1["fetch_errors"] == 1
    assert r1["duplicates"] == 0
    assert r1["reused_cache"] == 0
    assert r1["reread_updated"] == 0
    assert r1["new_vs_previous"] == 1        # bell = unico contenuto nuovo
    assert r1["docs_with_evidence"] == 1
    assert r1["unread_for_budget"] == 0
    assert not any(k.startswith("terminated") for k in r1)
    assert len(r1["reading"]) == 1
    rd = r1["reading"][0]
    assert rd["source_id"] == "src_bell"
    assert rd["content_len"] == len(T_BELL)
    assert rd["passages_read"] == 1
    assert rd["evidences_from"] == 2
    assert rd["full_or_partial"] == "SUCCESS"
    assert rd["duplicate"] is False
    # diagnosi DERIVATA: cap 1x5=5, provider restituisce 2 (< cap)
    assert "cap teorico 5 = 1 query eseguite × max_pages_per_query=5" \
        in r1["pages_limit_origin"]
    assert "2 risultati" in r1["pages_limit_origin"]
    assert "1 errori di fetch" in r1["pages_limit_origin"]
    # gap status salvato nel registro (sq1 parziale: 1 solo dominio)
    assert r1["gap_status"] is not None
    assert set(r1["gap_status"].values()) <= {"coperta", "parziale", "non_coperta"}
    assert "parziale" in r1["gap_status"].values()

    # ---- ciclo 2 ----
    assert r2["cycle"] == 2 and r2["role"] == "approfondimento"
    assert len(r2["queries_executed"]) == 1  # query generata per sq aperte
    assert r2["results_found"] == 2
    assert r2["candidates"] == 2
    assert r2["duplicates"] == 1             # mirror = stesso contenuto di bell
    assert r2["acquired_new"] == 1           # extra
    assert r2["fetch_errors"] == 0
    assert r2["new_vs_previous"] == 1        # solo il contenuto di extra
    assert r2["docs_with_evidence"] == 2     # mirror e extra prodotti evidenze
    # nel registro di lettura il duplicato e' marcato
    dup_rows = [e for e in r2["reading"] if e["source_id"] == "src_mirror"]
    assert dup_rows and dup_rows[0]["duplicate"] is True
    assert "1 duplicati di contenuto" in r2["pages_limit_origin"]
    # copertura avanzata: sq1 ora coperta (2 domini), sq2 parziale
    assert "coperta" in r2["gap_status"].values()

    # ---- persistenza nelle stats (runs.limits_json, chiave "cycles") ----
    saved = json.loads(storage.get_run(run_id)["limits_json"])
    assert set(saved["cycles"].keys()) == {"1", "2"}
    assert saved["cycles"]["1"]["acquired_new"] == 1
    assert saved["cycles"]["1"]["fetch_errors"] == 1
    assert saved["cycles"]["2"]["duplicates"] == 1
    assert saved["cycles"]["2"]["new_vs_previous"] == 1
    # il registro e' lo stesso oggetto emesso nell'evento
    assert saved["cycles"]["2"]["pages_limit_origin"] == r2["pages_limit_origin"]


async def test_read_docs_group_budget_warning(tmp_path, caplog):
    """Budget a gruppi: un gruppo con budget esaurito (0 evidenze) viene
    diagnosticato via log.warning senza cambiare il comportamento."""
    events: list = []

    class ScriptedReader:
        def __init__(self):
            self.calls = 0

        async def extract_evidences(self, question, subquestion_text, chunk,
                                    max_evidences=4, subquestion_id=None):
            self.calls += 1
            from app.models import Evidence
            return [Evidence(source_id=chunk.source_id, chunk_id=chunk.chunk_id,
                             quote=f"q {chunk.chunk_id}", claim="claim",
                             subquestion_id=subquestion_id or "")]

    settings = Settings(db_path=tmp_path / "g.db", cache_dir=tmp_path / "gc",
                        cache_enabled=False, _env_file=None)
    storage = Storage(settings.db_path)
    reader = ScriptedReader()
    orch = Orchestrator(settings, storage, EventBus(persist=events.append),
                        llm=None, searcher=None, fetcher=None, decisions=None,
                        planner=None, reader=reader, gap_checker=None,
                        writer=None, verifier=None)
    ctx = RunContext(run_id="run_groups")
    sq = SubQuestion(subquestion_id="s1", text="chi ha brevettato il telefono",
                     idx=1)

    def _doc(i):
        return FetchedDocument(source_id=f"srcG{i}", url=f"https://d{i}.example/p",
                               final_url=f"https://d{i}.example/p",
                               domain=f"d{i}.example",
                               text=f"testo della pagina numero {i} " * 30,
                               fetch_status=FetchStatus.SUCCESS)

    docs = [_doc(i) for i in range(6)]  # 2 gruppi: 5 + 1
    with caplog.at_level(logging.WARNING, logger="app.agent.orchestrator"):
        got = await orch._read_docs(ctx, "domanda", 1, docs, [sq],
                                    budget_box=[2])
    # budget 2 esaurito dai primi 2 doc: il 2o gruppo resta senza letture
    assert len(got) == 2
    assert reader.calls == 2
    acc = ctx.cycle_acc
    assert len(acc["reading"]) == 2
    assert all(r["passages_read"] == 1 for r in acc["reading"])
    assert acc["unread_for_budget"] == 4
    assert acc["docs_with_evidence"] == {"srcG0", "srcG1"}
    assert any("budget evidenze esaurito" in rec.getMessage()
               for rec in caplog.records)


async def test_read_docs_group_zero_evidence_warning(tmp_path, caplog):
    """Un gruppo letto PER INTERO ma con 0 evidenze produce il warning di
    diagnosticabilita' (budget ancora disponibile)."""
    events: list = []

    class EmptyReader:
        async def extract_evidences(self, question, subquestion_text, chunk,
                                    max_evidences=4, subquestion_id=None):
            return []

    settings = Settings(db_path=tmp_path / "e.db", cache_dir=tmp_path / "ec",
                        cache_enabled=False, _env_file=None)
    storage = Storage(settings.db_path)
    orch = Orchestrator(settings, storage, EventBus(persist=events.append),
                        llm=None, searcher=None, fetcher=None, decisions=None,
                        planner=None, reader=EmptyReader(), gap_checker=None,
                        writer=None, verifier=None)
    ctx = RunContext(run_id="run_empty")
    sq = SubQuestion(subquestion_id="s1", text="chi ha brevettato il telefono",
                     idx=1)
    docs = [FetchedDocument(source_id=f"srcE{i}", url=f"https://e{i}.example/p",
                            final_url=f"https://e{i}.example/p",
                            domain=f"e{i}.example", text="riempimento " * 60,
                            fetch_status=FetchStatus.SUCCESS)
            for i in range(3)]
    with caplog.at_level(logging.WARNING, logger="app.agent.orchestrator"):
        got = await orch._read_docs(ctx, "domanda", 1, docs, [sq])
    assert got == []
    assert len(ctx.cycle_acc["reading"]) == 3          # letti tutti
    assert all(r["evidences_from"] == 0 for r in ctx.cycle_acc["reading"])
    assert ctx.cycle_acc["docs_with_evidence"] == set()
    assert any("prodotto 0 evidenze" in rec.getMessage()
               for rec in caplog.records)
