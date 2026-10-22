"""B6 regression: contatori live durante la lettura, riuso chunk gia' letti,
contributo del ciclo. Offline: reader doppio, zero rete, zero API key.
"""
from __future__ import annotations

from pathlib import Path

from app.agent.orchestrator import Orchestrator, RunContext
from app.agent.search_state import SearchState
from app.config import Settings
from app.events.bus import EventBus
from app.models import (Evidence, FetchedDocument, FetchStatus, SubQuestion)
from app.storage import Storage


class ScriptedReader:
    """Reader double: 1 evidenza per chiamata; snapshots del contatore LIVE."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []  # (sq_text, chunk_id, source_id)
        self.stats_at_call: list[int] = []           # ctx.stats["evidences"] DURANTE la lettura
        self.ctx: RunContext | None = None

    async def extract_evidences(self, question, subquestion_text, chunk,
                                max_evidences=4, subquestion_id=None):
        self.calls.append((subquestion_text, chunk.chunk_id, chunk.source_id))
        self.stats_at_call.append(self.ctx.stats["evidences"] if self.ctx else -1)
        return [Evidence(source_id=chunk.source_id, chunk_id=chunk.chunk_id,
                         quote=f"q {chunk.chunk_id}", claim="claim",
                         subquestion_id=subquestion_id or "")]


def _build(tmp_path: Path, events: list):
    settings = Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "cache",
                        cache_enabled=False, usage_emit_every_s=0,
                        _env_file=None)
    storage = Storage(settings.db_path)
    bus = EventBus(persist=events.append)
    reader = ScriptedReader()
    orch = Orchestrator(settings, storage, bus, llm=None, searcher=None,
                        fetcher=None, decisions=None, planner=None, reader=reader,
                        gap_checker=None, writer=None, verifier=None)
    return orch, storage, reader


def _doc(source_id: str, domain: str, text: str) -> FetchedDocument:
    return FetchedDocument(source_id=source_id,
                           url=f"https://{domain}/pagina",
                           final_url=f"https://{domain}/pagina", domain=domain,
                           title=f"T {source_id}", text=text,
                           fetch_status=FetchStatus.SUCCESS)


SQ1 = SubQuestion(subquestion_id="s1", text="chi ha brevettato il telefono", idx=1)
SQ2 = SubQuestion(subquestion_id="s2", text="quando arrivò la radio", idx=2)


def _events(events: list, type_: str) -> list[dict]:
    return [e.payload for e in events if e.type == type_]


async def test_evidences_counter_is_live_per_doc(tmp_path):
    """Il contatore cresce DURANTE il ciclo di lettura (a ogni doc) e
    evidence_extracted esce per singolo documento, non a fine ciclo."""
    events: list = []
    orch, storage, reader = _build(tmp_path, events)
    ctx = RunContext(run_id="run_live")
    reader.ctx = ctx
    docs = [_doc("srcA", "a.example", "testo della pagina A " * 50),
            _doc("srcB", "b.example", "testo della pagina B " * 50)]
    got = await orch._read_docs(ctx, "domanda", 1, docs, [SQ1, SQ2])

    assert len(got) == 2
    assert ctx.stats["evidences"] == 2
    # LIVE: la 2a lettura vede il contatore GIA' a 1 (prima restava 0 a fine ciclo)
    assert reader.stats_at_call == [0, 1]
    # evento per documento: count parziale del doc, total cumulativo del run
    evs = _events(events, "evidence_extracted")
    assert [(p["count"], p["total"]) for p in evs] == [(1, 1), (1, 2)]
    # usage_update emesso durante la lettura (throttle a 0 = sempre)
    assert len(_events(events, "usage_update")) >= 2


async def test_reuse_stored_chunks_for_new_subquestion(tmp_path):
    """Fonte gia' letta al ciclo 1: al ciclo 2 i chunk arrivano da storage
    (nessun ri-download, nessun ri-chunkify) e l'estrazione usa SOLO la
    sotto-domanda aperta come angolo; source_reading marcato reused=True."""
    events: list = []
    orch, storage, reader = _build(tmp_path, events)
    ctx = RunContext(run_id="run_reuse")
    reader.ctx = ctx
    shared = SearchState()
    # al ciclo 1 il doc parla di brevetti -> best-match su SQ1 (testo < 4000
    # char: un chunk unico, come vuole il test)
    doc = _doc("srcA", "a.example",
               "brevetto telefono " * 100 + "riempimento generico " * 50)
    await orch._read_docs(ctx, "domanda", 1, [doc], [SQ1, SQ2],
                          shared_state=shared)
    used_after_read = shared.used_sq_by_source["srcA"]
    assert used_after_read  # il ciclo 1 ha consumato almeno un angolo
    n_calls_after_read = len(reader.calls)
    assert len(storage.chunks_for_source("srcA")) == 1  # chunk unico in storage

    # ciclo 2: SQ2 aperta e MAI usata su srcA -> riuso, zero fetch
    reused = await orch._reuse_chunks(ctx, "domanda", 2, [SQ2], shared, [10])
    assert len(reused) == 1
    assert ctx.stats["evidences"] == 2
    # il reader ha visto il chunk DI STORAGE (stesso chunk_id), col nuovo angolo
    sq_text, chunk_id, source_id = reader.calls[-1]
    assert (sq_text, source_id) == (SQ2.text, "srcA")
    assert chunk_id == storage.chunks_for_source("srcA")[0]["id"]
    assert len(reader.calls) == n_calls_after_read + 1  # nessuna lettura extra
    # nessun duplicato dei chunk in storage
    assert len(storage.chunks_for_source("srcA")) == 1
    # eventi marcati reused=True
    sr = _events(events, "source_reading")[-1]
    assert sr["reused"] is True and sr["subquestion"] == SQ2.text
    ee = _events(events, "evidence_extracted")[-1]
    assert ee["reused"] is True and ee["total"] == 2

    # lo stesso angolo non gira due volte sulla stessa fonte
    again = await orch._reuse_chunks(ctx, "domanda", 3, [SQ2], shared, [10])
    assert again == []
    assert len(reader.calls) == n_calls_after_read + 1


async def test_cycle_contribution_stats_and_event(tmp_path):
    events: list = []
    orch, storage, reader = _build(tmp_path, events)
    ctx = RunContext(run_id="run_contrib")
    ctx.stats["evidences"] = 5
    entry = orch._record_cycle_contribution(
        ctx, 3, ev_start=2, domains_before={"vecchio.example"},
        domains_now={"nuovo.example", "vecchio.example"})
    assert entry == {"new_evidences": 3, "new_domains": 1}
    assert ctx.stats["per_cycle"][3] == entry
    payload = _events(events, "cycle_contribution")
    assert payload == [{"new_evidences": 3, "new_domains": 1}]


async def test_read_docs_registers_sources_for_reuse(tmp_path):
    """_read_docs registra read_sources/used_sq_by_source (cache in-memory)."""
    events: list = []
    orch, storage, reader = _build(tmp_path, events)
    ctx = RunContext(run_id="run_reg")
    reader.ctx = ctx
    shared = SearchState()
    doc = _doc("srcA", "a.example", "brevetto telefono storia " * 200)
    await orch._read_docs(ctx, "domanda", 1, [doc], [SQ1], shared_state=shared)
    meta = shared.read_sources["srcA"]
    assert meta["url"] == "https://a.example/pagina"
    assert meta["domain"] == "a.example"
    assert meta["cycle"] == 1
    assert shared.used_sq_by_source["srcA"] == {"s1"}
