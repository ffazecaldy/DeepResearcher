"""Orchestrator: iterative research pipeline (plan→search→fetch→decide→read→gap→write→verify)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Depth
from app.agent.query_generator import QueryGenerator
from app.agent.search_state import SearchState, cycle_role
from app.events.bus import EventBus
from app.limits import LimitReached, RuntimeBudget
from app.models import (Chunk, DecisionKind, DecisionSpec, Event, RunOutcome,
                        SearchResultItem, SubQuestion)
from app.provenance.tracker import ProvenanceTracker
from app.security import wrap_external  # noqa: F401  (re-exported for tests)

log = logging.getLogger(__name__)

_DEPTH_SUBQ = {Depth.RAPIDA: 3, Depth.STANDARD: 4, Depth.APPROFONDITA: 6}
_MAX_EVIDENCES_PER_CYCLE = 60
# Lettura a piccoli gruppi: il contributo viene rivalutato tra un gruppo e
# l'altro (budget residuo / doc rimanenti) e un gruppo muto e' diagnosticato.
_READ_GROUP_DOCS = 5


def _unwrap(x):
    """Accept either the object or a (object, closer) tuple from bootstrap."""
    return x[0] if isinstance(x, tuple) else x


@dataclass
class RunContext:
    """Per-run isolated state (P0-4): the orchestrator instance is shared
    across concurrent runs; NOTHING mutable lives on self anymore."""
    run_id: str
    no_novelty: int = 0
    snowball: object = None          # SnowballState (created per run)
    stats: dict = field(default_factory=lambda: {"sources": 0, "evidences": 0})
    cycle: int = 0
    last_usage_emit: float = 0.0
    timings: list = field(default_factory=list)
    cycle_tokens_in: int = 0
    cycle_tokens_out: int = 0
    # Registro di ciclo (cycle_registry): accumulatori del ciclo in corso.
    cycle_acc: dict = field(default_factory=dict)
    # content_hash nuovi per ciclo (per new_vs_previous del registro)
    hashes_by_cycle: dict = field(default_factory=dict)
    # norm_url -> primo ciclo in cui la fonte e' stata acquisita
    # (rileva ri-letture di URL noti con contenuto aggiornato)
    url_seen_cycle: dict = field(default_factory=dict)


class Orchestrator:
    def __init__(self, settings, storage, bus: EventBus, llm,
                 searcher, fetcher, decisions, planner, reader,
                 gap_checker, writer, verifier, query_generator=None):
        self.settings = settings
        self.storage = storage
        self.bus = bus
        self.llm = llm
        self.searcher = _unwrap(searcher)
        self.fetcher = _unwrap(fetcher)
        self.decisions = decisions
        self.planner = planner
        self.reader = reader
        self.gap_checker = gap_checker
        self.writer = writer
        self.verifier = verifier
        self.query_generator = query_generator or QueryGenerator(llm)

    # ---------- termination (SINGLE decision point) ----------
    def _should_stop(self, *, ctx, cycle: int, effective: int, budget,
                     gap_report, new_urls: int, new_domains: int,
                     shared_state: SearchState) -> tuple[bool, str, str]:
        """The ONLY place where the cycle loop may end early. Returns
        (stop, reason, detail); every reason is logged and surfaced."""
        # (a) complete coverage of every sub-question (decided in code)
        if gap_report.complete:
            return True, "COPERTURA_COMPLETA", (
                "tutte le sotto-domande hanno almeno 2 fonti indipendenti")
        # (c) declared limits
        if budget.time_left() <= 0:
            return True, "LIMITE_TEMPO", f"max_runtime_seconds={budget.max_runtime_seconds}"
        if budget.pages_fetched >= budget.max_total_pages:
            return True, "LIMITE_PAGINE", f"max_total_pages={budget.max_total_pages}"
        # (d) two consecutive cycles without relevant novelty
        if cycle >= 2 and new_urls == 0:
            ctx.no_novelty += 1
        else:
            ctx.no_novelty = 0
        if ctx.no_novelty >= 2:
            return True, "NESSUNA_NOVITA", (
                "due cicli consecutivi senza URL nuovi dopo diversificazione")
        # (e) unsolvable: >=2 diversification attempts for every open sub-question
        open_ids = (set(gap_report.status_by_id)
                    - set(gap_report.covered_subquestions))
        unsolvable = open_ids and all(
            shared_state.attempts_by_sq.get(sid, 0) >= 2 for sid in open_ids)
        if unsolvable:
            return True, "SOTTODOMANDE_NON_RISOLVIBILI", (
                f"sotto-domande non coperte dopo 2 tentativi: {sorted(open_ids)}")
        # (b) otherwise: continue until MAX_CICLI (loop ends naturally)
        return False, "", ""

    # ---------- events ----------
    def emit(self, ctx, type_: str, cycle: int, **payload) -> None:
        self.bus.publish(Event(type=type_, run_id=ctx.run_id, cycle=cycle,
                               payload=payload))

    def _snowball_queries(self, ctx, cycle: int, seen_urls: set[str]):
        """B4: i candidate snowball diventano query "site-direct" (URL fetch).

        Priorità bassissima: entrano DOPO gen_queries e gap_new_queries e solo
        finché c'è posto sotto max_queries_per_cycle.
        """
        from app.models import GeneratedQuery
        from app.security import normalize_for_dedupe
        seen_norm = {normalize_for_dedupe(u) for u in seen_urls}
        out = []
        for url in ctx.snowball.take_pending(seen_norm):
            out.append(GeneratedQuery(
                query_id=f"{ctx.run_id[:24]}_sb{cycle}_{len(out) + 1}",
                subquestion_id="", text=url, cycle=cycle))
            seen_norm.add(normalize_for_dedupe(url))
        return out

    # ---------- run ----------
    async def run(self, question: str, language: str, depth: Depth, run_id: str,
                  cancel: asyncio.Event) -> RunOutcome:
        ctx = RunContext(run_id=run_id)
        decisions = (self.decisions.bind(run_id)
                     if hasattr(self.decisions, "bind") else self.decisions)
        self.storage.usage_init(run_id)
        self.storage.create_run(run_id, question, language, depth.value)
        self.emit(ctx, "run_started", 0, question=question, depth=depth.value,
                  language=language)
        budget = RuntimeBudget(max_runtime_seconds=self.settings.max_runtime_seconds,
                               max_total_pages=self.settings.max_total_pages)
        seen_urls: set[str] = set()  # never re-pick pages already read
        seen_content: set[str] = set()  # B6: content_hash gia' visti nel run
        effective = self.settings.cycle_budget(depth)
        log.debug(
            "[CYCLES] depth=%s | DR_MAX_CYCLES=%s | preset=%s | EFFETTIVO=%s "
            "(min dei due) | max_total_pages=%s | max_runtime=%ss",
            depth.value, self.settings.max_cycles, _DEPTH_SUBQ.get(depth),
            effective, self.settings.max_total_pages, self.settings.max_runtime_seconds)
        shared_state = SearchState()
        # B4: candidate URL da snowballing (link citati dalle fonti accettate)
        from app.agent.snowball import SnowballState
        ctx.snowball = SnowballState()
        # B5: budget tempo per ciclo (0 = disattivo) + token/step per ciclo
        per_cycle_s = getattr(self.settings, "max_seconds_per_cycle", 0) or 0
        cycle_deadline = [0.0]
        cycle_tokens_start = [0]
        stop_reason: str = "MAX_CICLI"  # default when the loop ends naturally
        stop_detail: str = ""
        domain_by_source: dict[str, str] = {}
        status = "completed"
        error: str | None = None
        limit_note = ""
        plan = None
        evidences = []
        subquestions: list[SubQuestion] = []
        accepted_docs = []
        accepted: list = []  # per-cycle; definita anche quando `items` è vuoto
        report_md = ""
        report_claims = []
        attempts = 0  # writer language regenerations

        try:
            for cycle in range(1, self.settings.cycle_budget(depth) + 1):
                log.debug("[CICLO %s] avvio (max=%s)", cycle, effective)
                if cancel.is_set():
                    status = "cancelled"
                    stop_reason, stop_detail = "ANNULLATO", "richiesta utente"
                    break
                try:
                    budget.check_time()
                except LimitReached as exc:
                    limit_note = str(exc)
                    stop_reason, stop_detail = "LIMITE_TEMPO", str(exc)
                    break
                ctx.cycle = cycle
                # B4: ruolo del ciclo nel payload (esplorazione/approfondimento/verifica)
                self.emit(ctx, "cycle_started", cycle, role=cycle_role(cycle))
                # Registro di ciclo: accumulatori del ciclo in corso (consumati
                # da _emit_cycle_registry a fine ciclo)
                ctx.cycle_acc = {
                    "queries": [],
                    "results_found": 0,
                    "candidates": 0,
                    "acquired_new": 0,
                    "reused_cache": 0,
                    "reused_evidences": 0,
                    "reread_updated": 0,
                    "reread_motivo": "",
                    "duplicates": 0,
                    "fetch_errors": 0,
                    "docs_with_evidence": set(),
                    "duplicate_ids": set(),
                    "reading": [],
                    "unread_for_budget": 0,
                }
                # B5: deadline di ciclo + snapshot token a inizio ciclo
                cycle_deadline[0] = (time.monotonic() + per_cycle_s
                                     if per_cycle_s > 0 else 0.0)
                cycle_tokens_start[0] = (getattr(self.llm, "tokens_in", 0)
                                         + getattr(self.llm, "tokens_out", 0))
                ctx.cycle_tokens_in = 0
                ctx.cycle_tokens_out = 0
                # B6: snapshot a inizio ciclo per il contributo del ciclo
                ev_at_cycle_start = ctx.stats["evidences"]
                domains_at_cycle_start = set(shared_state.domain_count)
                contribution_done = False

                # ---- planning (first cycle only) ----
                if plan is None:
                    self.emit(ctx, "planning_started", cycle,
                          note="Il bot sta analizzando la domanda e scegliendo le pagine")
                    # B5: la lingua del piano segue la lingua GLOBALE del report,
                    # non la lingua della domanda (coerenza titolo/sotto-domande/report)
                    plan_lang = (self.settings.report_language
                                 or language or "it").strip() or "it"
                    plan = await self.planner.make_plan(
                        question, plan_lang,
                        max_subquestions=_DEPTH_SUBQ[depth])
                    # ids are only unique within a plan: prefix with run_id
                    # (SQLite PKs are table-global, runs share the DB)
                    prefix = run_id[:24]
                    # P1-5: remap query.subquestion_id to the prefixed ids so
                    # the persisted chain subquestion->query stays joinable
                    remap = {}
                    for i, sq in enumerate(plan.subquestions, start=1):
                        remap[sq.subquestion_id] = f"{prefix}_sq{i}"
                        sq.subquestion_id = remap[sq.subquestion_id]
                    for i, q in enumerate(plan.queries, start=1):
                        q.query_id = f"{prefix}_q{i}"
                        q.subquestion_id = remap.get(q.subquestion_id,
                                                     q.subquestion_id)
                    subquestions = plan.subquestions
                    for sq in subquestions:
                        self.storage.add_subquestion(run_id, sq.subquestion_id,
                                                     sq.idx, sq.text)
                    cap = self.settings.max_queries_per_cycle
                    for q in plan.queries[:cap]:
                        self.storage.add_query(run_id, q.query_id,
                                               q.subquestion_id, q.text, 1,
                                               self.settings.search_provider.value)
                    self.emit(ctx, "plan_generated", cycle, title=plan.title,
                              subquestions=[s.text for s in subquestions],
                              queries=[q.text for q in plan.queries[:cap]])

                # ---- search ----
                _t_search = time.perf_counter()
                # B6: sotto-domande aperte (dal gap report del ciclo precedente);
                # vuote al ciclo 1 (non c'e' ancora nulla da riusare)
                open_sqs: list[SubQuestion] = []
                if cycle == 1:
                    queries = plan.queries[:self.settings.max_queries_per_cycle]
                else:
                    # BUG2: query diversificate SOLO sulle sotto-domande aperte
                    open_ids = set(report_status_by_id) - set(report_covered_ids)
                    open_sqs = [s for s in subquestions
                                if s.subquestion_id in open_ids]
                    for s in open_sqs:
                        shared_state.attempts_by_sq[s.subquestion_id] = \
                            shared_state.attempts_by_sq.get(s.subquestion_id, 0) + 1
                    gen_queries = await self.query_generator.generate(
                        question, open_sqs, shared_state, cycle)
                    # B13: gli ID generati dal QueryGenerator (es. "g2_1") sono
                    # unici solo dentro il run: senza prefisso collidono tra run
                    # diversi sulla PK globale di queries
                    for i, gq in enumerate(gen_queries):
                        gq.query_id = f"{run_id[:24]}_g{cycle}_{i}"
                    all_q = (gen_queries
                             + [_as_query(ctx.run_id, q.subquestion_id, q.text,
                                          cycle, i)
                                for i, q in enumerate(gap_new_queries)]
                             + self._snowball_queries(ctx, cycle, seen_urls))
                    seen_texts = set()
                    queries = []
                    for q in all_q:
                        if shared_state.already_ran(q.text):
                            continue
                        if q.text.lower() in seen_texts:
                            continue
                        seen_texts.add(q.text.lower())
                        queries.append(q)
                    queries = queries[:self.settings.max_queries_per_cycle]
                    for q in queries:
                        shared_state.see_query(q.text)
                        # P1-5: queries of later cycles are persisted too
                        self.storage.add_query(
                            run_id, q.query_id, q.subquestion_id, q.text, cycle,
                            self.settings.search_provider.value)
                log.debug("[CICLO %s/%s] query: %s", cycle, effective,
                          [q.text for q in queries])
                # B13 difesa attiva: ID duplicati non devono MAI raggiungere la
                # PK di queries (collisione = run fallito); rigenera i collidenti
                seen_ids = set()
                for q in queries:
                    if q.query_id in seen_ids or not q.query_id:
                        q.query_id = (f"{run_id[:24]}_x{cycle}_"
                                      f"{len(seen_ids)}_{abs(hash(q.text)) % 10_000}")
                    seen_ids.add(q.query_id)
                # P1-8: snowball URLs (http… nel testo della query) sono
                # fetchati DIRETTAMENTE con le stesse protezioni/budget del
                # fetch ordinario — non passano dal motore di ricerca
                url_items = []
                text_queries = []
                for q in queries:
                    if q.text.lower().startswith(("http://", "https://")):
                        url_items.append(SearchResultItem(
                            query_id=q.query_id, url=q.text,
                            title="snowball link", position=0))
                    else:
                        text_queries.append(q)
                self.emit(ctx, "query_started", cycle, count=len(queries))
                # registro: le query ESEGUITE nel ciclo (testi, dopo dedup/cap)
                ctx.cycle_acc["queries"] = [q.text for q in queries]
                items = list(await self.searcher.run_queries(
                    text_queries, self.settings.max_pages_per_query))
                if url_items:
                    items.extend(url_items)
                # registro: risultati GREZZI del provider (prima del filtro URL visti)
                ctx.cycle_acc["results_found"] = len(items)
                # P1-5: persist query -> result links (multiple queries may hit
                # the same URL: every pair is stored, never collapsed)
                for q in queries:
                    pos = 0
                    for it in items:
                        if it.query_id == q.query_id:
                            self.storage.add_search_result(
                                run_id, q.query_id, it.url, it.url, it.title, pos)
                            pos += 1
                items = [it for it in items
                         if it.url not in seen_urls]  # no re-reads across cycles
                ctx.cycle_acc["candidates"] = len(items)
                # B4: scala di novità — prima i dominio-nuovi, poi i già-visti
                from app.agent.search_state import novelty_score as _novelty
                items.sort(key=lambda it: _novelty(it.url, shared_state),
                           reverse=True)
                domains = {}
                for it in items:
                    from urllib.parse import urlsplit
                    d = (urlsplit(it.url).hostname or "?")
                    domains[d] = domains.get(d, 0) + 1
                log.debug("[CICLO %s] risultati DOPO filtro seen_urls: %s | domini: %s",
                          cycle, len(items), domains)
                new_urls = len(items)
                self.emit(ctx, "search_result_found", cycle, count=len(items))
                t_search = time.perf_counter() - _t_search
                ctx.timings.append({"cycle": cycle, "step": "search",
                                      "s": round(t_search, 1)})
                log.debug("[TEMPI] ciclo %s search=%.1fs", cycle, t_search)
                self._maybe_emit_usage(ctx)
                if items:
                    _t_fetch = time.perf_counter()
                    def _fetch_event(kind: str, url: str) -> None:
                        # registro: pagine servite dalla cache disco (nessun download)
                        if kind == "source_cache_hit":
                            ctx.cycle_acc["reused_cache"] = (
                                ctx.cycle_acc.get("reused_cache", 0) + 1)
                        self.emit(ctx, kind, cycle, url=url)
                    try:
                        docs = await self.fetcher.fetch_all(items, budget,
                                                            on_event=_fetch_event)
                    except LimitReached as exc:
                        docs = []
                        limit_note = str(exc)
                    for doc in docs:
                        self._persist_source(run_id, doc)
                        if doc.fetch_status.value.startswith(("SUCCESS", "PARTIAL")):
                            # B6/B12: contenuto gia' visto nel run (anche su URL
                            # diverso) = duplicato: NON si rilegge e NON conta
                            # come fonte indipendente (10 copie != 10 conferme)
                            is_dup = (bool(doc.content_hash)
                                      and doc.content_hash in seen_content)
                            if doc.content_hash:
                                seen_content.add(doc.content_hash)
                            if is_dup:
                                ctx.cycle_acc["duplicates"] += 1
                                ctx.cycle_acc["duplicate_ids"].add(doc.source_id)
                                self.emit(ctx, "source_duplicate", cycle,
                                          source_id=doc.source_id,
                                          url=doc.final_url or doc.url,
                                          content_hash=doc.content_hash)
                                continue
                            # registro: URL gia' letto in un ciclo precedente ma
                            # con contenuto nuovo = pagina aggiornata (ri-lettura
                            # con motivo); hash nuovo rispetto a TUTTI i cicli
                            # precedenti = documento unico (new_vs_previous)
                            norm = shared_state.norm_url(doc.final_url or doc.url)
                            first_cycle = ctx.url_seen_cycle.get(norm)
                            if first_cycle:
                                ctx.cycle_acc["reread_updated"] += 1
                                ctx.cycle_acc["reread_motivo"] = (
                                    f"URL gia' letto al ciclo {first_cycle}, "
                                    "contenuto aggiornato (content_hash nuovo)")
                            if doc.content_hash:
                                ctx.hashes_by_cycle.setdefault(
                                    cycle, set()).add(doc.content_hash)
                            ctx.url_seen_cycle[norm] = cycle
                            ctx.cycle_acc["acquired_new"] += 1
                            ctx.stats["sources"] += 1
                            self.emit(ctx, "source_fetched", cycle,
                                      source_id=doc.source_id,
                                      url=doc.final_url or doc.url,
                                      reused=False)
                        else:
                            ctx.cycle_acc["fetch_errors"] += 1
                            self.emit(ctx, "source_failed", cycle, source_id=doc.source_id,
                                      url=doc.url, error=doc.error)
                    accepted = await self._evaluate_docs(run_id, cycle, docs,
                                                         question, decisions, ctx)
                    # B4/P1-8: snowballing — link <a href> del RAW HTML delle
                    # pagine accettate (non del testo estratto)
                    for doc in accepted:
                        for u in (doc.links or [])[:10]:
                            ctx.snowball.propose(u)
                    t_fetch = time.perf_counter() - _t_fetch
                    ctx.timings.append({"cycle": cycle, "step": "fetch+judge",
                                          "s": round(t_fetch, 1)})
                    log.debug("[TEMPI] ciclo %s fetch+judge=%.1fs", cycle, t_fetch)
                    accepted_docs.extend(accepted)
                    _t_read = time.perf_counter()
                    # B6: budget evidenze condiviso tra read e riuso nel ciclo
                    cycle_budget_box = [_MAX_EVIDENCES_PER_CYCLE]
                    new_evs = await self._read_docs(ctx, question, cycle, accepted,
                                                    subquestions,
                                                    shared_state=shared_state,
                                                    budget_box=cycle_budget_box)
                    evidences.extend(new_evs)
                    # B6: riuso — chunk gia' in storage per fonti lette nei
                    # cicli precedenti, SOLO con l'angolo delle sotto-domande
                    # ora aperte; nessun ri-download
                    reused_evs = await self._reuse_chunks(
                        ctx, question, cycle, open_sqs, shared_state,
                        cycle_budget_box)
                    evidences.extend(reused_evs)
                    # registro: evidenze prodotte dal RIUSO di chunk gia' in storage
                    ctx.cycle_acc["reused_evidences"] = len(reused_evs)
                    ctx.timings.append({"cycle": cycle, "step": "read(LLM)",
                                          "s": round(time.perf_counter() - _t_read, 1)})
                    log.debug("[TEMPI] ciclo %s read=%.1fs", cycle,
                              time.perf_counter() - _t_read)
                    self._maybe_emit_usage(ctx)
                    # B6: contributo del ciclo — emesso SUBITO dopo la lettura
                    # cosi' arriva nel DB anche se la deadline interrompe qui
                    self._record_cycle_contribution(
                        ctx, cycle, ev_at_cycle_start, domains_at_cycle_start,
                        {m.get("domain", "") for m in
                         shared_state.read_sources.values()
                         if m.get("cycle") == cycle})
                    contribution_done = True
                    # B5: check deadline per-ciclo (subito dopo il primo step pesante)
                    if cycle_deadline[0] and time.monotonic() > cycle_deadline[0]:
                        limit_note = (limit_note or
                                      f"max_seconds_per_cycle={per_cycle_s} al ciclo {cycle}")
                        stop_reason, stop_detail = "LIMITE_CICLO", (
                            f"budget per ciclo esaurito: {per_cycle_s}s al ciclo {cycle}")
                        log.debug("[TERMINAZIONE] %s — %s", stop_reason, stop_detail)
                        self._emit_cycle_registry(ctx, cycle, shared_state,
                                                  early="budget per-ciclo "
                                                        "esaurito prima del gap check")
                        self.emit(ctx, "cycle_completed", cycle, complete=True,
                                  stop_reason=stop_reason, stop_detail=stop_detail)
                        break

                    # P1-9: cooperative deadline — also checked between fetch
                    # and read (a long read must not start past the deadline),
                    # and writer/verification get a RESERVED tail of the budget
                    if cancel.is_set():
                        status = "cancelled"
                        stop_reason, stop_detail = "ANNULLATO", "richiesta utente"
                        self._emit_cycle_registry(ctx, cycle, shared_state,
                                                  early="annullato prima del "
                                                        "gap check")
                        break
                    if cycle_deadline[0] and time.monotonic() > cycle_deadline[0]:
                        limit_note = (limit_note or
                                      f"max_seconds_per_cycle={per_cycle_s} al ciclo {cycle}")
                        stop_reason, stop_detail = "LIMITE_CICLO", (
                            f"budget per ciclo esaurito: {per_cycle_s}s al ciclo {cycle}")
                        log.debug("[TERMINAZIONE] %s — %s", stop_reason, stop_detail)
                        self._emit_cycle_registry(ctx, cycle, shared_state,
                                                  early="budget per-ciclo "
                                                        "esaurito prima del gap check")
                        self.emit(ctx, "cycle_completed", cycle, complete=True,
                                  stop_reason=stop_reason, stop_detail=stop_detail)
                        break

                # B6: ciclo senza nuovi documenti — contributo a zero (il evento
                # deve esistere nel DB per ogni ciclo con search conclusa)
                if not contribution_done:
                    self._record_cycle_contribution(
                        ctx, cycle, ev_at_cycle_start, domains_at_cycle_start,
                        set())

                # ---- gap check (coverage decided in code) ----
                _t_gap = time.perf_counter()
                self.emit(ctx, "gap_check_started", cycle)
                for doc in accepted:
                    seen_urls.add(doc.url)
                    shared_state.add_url(doc.final_url or doc.url)
                    domain_by_source[doc.source_id] = doc.domain
                report = await self.gap_checker.check(question, subquestions,
                                                      evidences,
                                                      domain_of=domain_by_source)
                log.debug("[CICLO %s] GAP complete=%s status=%s", cycle,
                          report.complete, report.status_by_id)
                gap_new_queries = list(report.new_queries)
                report_status_by_id = dict(report.status_by_id)
                report_covered_ids = set(report.covered_subquestions)
                ctx.timings.append({"cycle": cycle, "step": "gap_check(LLM)",
                                      "s": round(time.perf_counter() - _t_gap, 1)})
                log.debug("[TEMPI] ciclo %s gap=%.1fs", cycle,
                          time.perf_counter() - _t_gap)

                # ---- registro di ciclo (consultabile) ----
                # emesso PRIMA del punto di terminazione: anche l'ultimo ciclo
                # porta il suo registro, con lo stato di copertura del gap check
                self._emit_cycle_registry(ctx, cycle, shared_state,
                                          gap_status=report.status_by_id)

                # ---- UNIQUE termination point ----
                stop, stop_reason, stop_detail = self._should_stop(
                    ctx=ctx, cycle=cycle, effective=effective, budget=budget,
                    gap_report=report, new_urls=new_urls,
                    new_domains=len({d for d in domains}),
                    shared_state=shared_state)
                if stop:
                    log.debug("[TERMINAZIONE] %s — %s", stop_reason, stop_detail)
                    self.emit(ctx, "cycle_completed", cycle, complete=True,
                              stop_reason=stop_reason, stop_detail=stop_detail)
                    break
                self.emit(ctx, "gap_detected", cycle,
                          missing=report.missing_subquestions,
                          partial=report.partial_subquestions,
                          status=report.status_by_id,
                          contradictions=report.contradictions,
                          new_queries=[q.model_dump() for q in report.new_queries])
                self.emit(ctx, "cycle_completed", cycle)

            # ---- writing ----
            # P1-9: reserve a tail of the budget for write+verify; only start
            # writing if at least the reserve is left (or the cap is disabled)
            reserve_s = max(60.0, budget.max_runtime_seconds * 0.2) \
                if budget.max_runtime_seconds > 0 else 0.0
            if evidences and not cancel.is_set() and (
                    budget.max_runtime_seconds <= 0
                    or budget.time_left() >= reserve_s
                    or status == "completed" and budget.time_left() > 0):
                t_write = time.perf_counter()
                out_lang = self.settings.report_language or language
                log.debug("[WRITER] language=%s | evidenze totali=%s",
                          out_lang, len(evidences))
                self.emit(ctx, "writing_started", 0,
                          note="Il bot sta ragionando su come impostare il report")
                self._maybe_emit_usage(ctx)
                await asyncio.sleep(0.8)  # cede il loop: la UI vede la fase
                limit_note = (limit_note if
                              stop_reason.startswith("LIMITE_") else
                              f"Terminazione: {stop_reason} — {stop_detail}")
                draft = await self.writer.write(question, out_lang, subquestions,
                                                evidences, limit_note=limit_note)
                # BUG3: automatic language check, max 2 regenerations
                from app.lang_check import looks_italian
                attempts = 0
                while (out_lang.startswith("it") and not looks_italian(draft.markdown)
                       and attempts < 2):
                    attempts += 1
                    log.warning("writer: report non in italiano (tentativo %s), "
                                "rigenerazione", attempts)
                    draft = await self.writer.write(
                        question, out_lang, subquestions, evidences,
                        limit_note=limit_note)
                log.debug("[WRITER] completato in %.1fs (rigenerazioni=%s, "
                          "italiano=%s)", time.perf_counter() - t_write, attempts,
                          looks_italian(draft.markdown))
                for c in draft.claims:
                    self.storage.add_claim(run_id, c.claim_id, c.text,
                                           c.citation_no, c.evidence_ids)
                # P0-1: la bozza viaggia sulla UI con un evento dedicato; il
                # testo definitivo arriva solo dopo la verifica (report_final)
                self.emit(ctx, "report_chunk", 0, title=draft.title,
                          markdown=draft.markdown, phase="draft")

                # ---- verification ----
                self.emit(ctx, "verification_started", 0,
                          note="Il bot sta ricontrollando ogni frase sulle fonti")
                ev_map = {e.evidence_id: e for e in evidences}
                src_map = {d.source_id: d for d in accepted_docs}
                verified = await self.verifier.verify(question, draft, ev_map, src_map)
                for c in verified.claims:
                    self.storage.add_verification(run_id, c.claim_id,
                                                  c.verdict or "FAILED",
                                                  c.verdict_reason or "",
                                                  c.corrected_text)
                    ev_name = {"VERIFIED": "claim_verified", "CORRECTED": "claim_corrected",
                               "REMOVED": "claim_removed"}.get(c.verdict, "claim_failed")
                    self.emit(ctx, ev_name, 0, claim_id=c.claim_id, reason=c.verdict_reason)
                # update claims with verdict + unified final number (P0-2)
                for c in verified.claims:
                    self.storage._exec(
                        "UPDATE claims SET final_citation_no=? WHERE id=?",
                        (c.final_citation_no, c.claim_id))
                # B12: stati di verifica (supportata/contraddetta/insufficiente/
                # contrastante) dai prefissi [support:X] scritti dal verifier —
                # resi visibili nel report, mai nascosti
                support_stats = _count_support(verified.claims)
                verified_body = verified.markdown
                if support_stats and any(support_stats.values()):
                    verified_body += (
                        "\n\n> Stati delle affermazioni: "
                        f"{support_stats['supported']} supportate, "
                        f"{support_stats['contradicted']} contraddette, "
                        f"{support_stats['insufficient']} evidenze insufficienti, "
                        f"{support_stats['mixed']} contrastate\n")
                # P0-1: the FINAL text is the verified one — persisted, exported,
                # streamed. Draft corrections can no longer be thrown away.
                report_md = _final_markdown(draft.title, verified_body,
                                            verified.claims, evidences, src_map)
                report_claims = verified.claims
                self.emit(ctx, "report_final", 0, title=draft.title,
                          markdown=report_md,
                          verification={
                              "claims": verified.stats.number_of_claims,
                              "verified": verified.stats.number_verified,
                              "corrected": verified.stats.number_corrected,
                              "removed": verified.stats.number_removed,
                              "failed": verified.stats.number_failed,
                              "uncovered": verified.stats.number_uncovered,
                          })
            elif not evidences and not cancel.is_set():
                # B1-fix5: zero evidenze (es. search provider rate-limitato) ->
                # il run finiva "completed" senza report e senza spiegazione.
                # Stato onesto: failed con errore leggibile in UI.
                status = "failed"
                error = ("nessuna evidenza raccolta: il motore di ricerca non ha "
                         "restituito risultati (provider rate-limitato o offline?)")
                self.emit(ctx, "run_failed", 0, error=error)
            elif cancel.is_set():
                status = "cancelled"
        except LimitReached as exc:
            limit_note = str(exc)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - the run must end in a recorded state
            log.exception("run %s failed", run_id)
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            self.emit(ctx, "run_failed", 0, error=error)

        # ---- persist report / outcome ----
        md_path = json_path = None
        if report_md:
            report_id = f"rep_{run_id}"
            self.storage.save_report(run_id, report_id, question[:120], report_md)
            out_dir = Path("reports")
            out_dir.mkdir(exist_ok=True)
            md_path = str(out_dir / f"{run_id}.md")
            Path(md_path).write_text(report_md, encoding="utf-8")
            json_path = str(out_dir / f"{run_id}.json")
            self._export_json(run_id, question, json_path, report_md, report_claims,
                              evidences)
        if cancel.is_set():
            status = "cancelled"
        # B5: stats (incl. per_cycle) — built BEFORE any persistence that uses it
        stats = {
            "sources": len(accepted_docs),
            "evidences": len(evidences),
            "claims": len(report_claims),
            "decisions": getattr(decisions, "counts", {}),
            "stop_reason": stop_reason,
            "stop_detail": stop_detail,
            "timings": ctx.timings,
        }
        # B5: tabella token/step per ciclo (aggregata dai timing + contatori LLM)
        by_cycle: dict[int, dict[str, float]] = {}
        for row in ctx.timings:
            c = by_cycle.setdefault(row["cycle"], {"seconds": 0.0})
            c["seconds"] += row.get("s", 0.0)
        for cyc, agg in sorted(by_cycle.items()):
            agg["steps"] = sum(1 for r in ctx.timings if r["cycle"] == cyc)
        stats["per_cycle"] = by_cycle
        # B6: contributo per ciclo (nuove evidenze / nuovi domini) — registrato
        # live durante il run, qui merged negli aggregati persistiti
        for cyc, contribution in ctx.stats.get("per_cycle", {}).items():
            agg = by_cycle.setdefault(cyc, {"seconds": 0.0})
            agg["new_evidences"] = contribution.get("new_evidences", 0)
            agg["new_domains"] = contribution.get("new_domains", 0)
        # registro di ciclo consultabile (ciclo -> payload cycle_registry),
        # persistito in runs.limits_json sotto la chiave "cycles"
        stats["cycles"] = ctx.stats.get("cycles_registry", {})
        # usage counters (llm client counters are process-cumulative: exact in CLI,
        # approximate for a long-lived server sharing one client across runs)
        self.storage.usage_inc(
            run_id,
            llm_calls=getattr(self.llm, "llm_calls", 0),
            llm_tokens_in=getattr(self.llm, "tokens_in", 0),
            llm_tokens_out=getattr(self.llm, "tokens_out", 0),
            laya_decisions=getattr(decisions, "counts", {}).get("laya", 0),
            llm_decisions=getattr(decisions, "counts", {}).get("llm", 0),
        )
        # P1-9: limit_note only for REAL limits, never for normal termination
        real_limit = stop_reason.startswith("LIMITE_") or limit_note.startswith(
            ("max_", "Limite", "budget"))
        self.storage.finish_run(run_id, status, error,
                                bool(real_limit), limit_note if real_limit else "")
        stats["limit_reached"] = real_limit
        stats["stop_reason"] = stop_reason
        # P1-9: persistence errors must surface, not vanish
        self.storage._exec("UPDATE runs SET limits_json=? WHERE id=?",
                           (json.dumps(stats, ensure_ascii=False), run_id))
        self.emit(ctx, "run_completed" if status == "completed" else
                  ("run_cancelled" if status == "cancelled" else "run_failed"),
                  0, status=status, limit_note=limit_note if real_limit else "",
                  stop_reason=stop_reason)
        usage = self.storage.usage_for_run(run_id) or {}
        stats["llm_calls"] = getattr(self.llm, "llm_calls", 0)
        stats["llm_tokens_in"] = getattr(self.llm, "tokens_in", 0)
        stats["llm_tokens_out"] = getattr(self.llm, "tokens_out", 0)
        stats["usage_db"] = usage
        return RunOutcome(run_id=run_id, status=status, question=question,
                          language=language, markdown_path=md_path,
                          json_path=json_path, limit_reached=real_limit,
                          limit_note=limit_note if real_limit else "",
                          stats=stats)

    # ---------- steps ----------
    def _persist_source(self, run_id: str, doc) -> None:
        self.storage.add_source(
            doc.source_id, run_id, doc.url, doc.final_url, doc.domain, doc.title,
            doc.http_status, doc.content_type, doc.fetch_status.value,
            doc.published_at, doc.author, doc.redirects, doc.error, doc.byte_len,
            content_hash=getattr(doc, "content_hash", ""))

    async def _evaluate_docs(self, run_id, cycle, docs, question, decisions, ctx):
        """Typed decisions per document: is_relevant + source_type + quality."""
        accepted = []
        pending = [d for d in docs
                   if d.fetch_status.value in ("SUCCESS", "PARTIAL_CONTENT")]
        specs = [
            DecisionSpec(name="is_relevant", kind=DecisionKind.BINARY,
                         question=f"Does this document contain information useful "
                                  f"to answer: {question}?"),
            DecisionSpec(name="source_type", kind=DecisionKind.CHOICE,
                         question="What kind of source is this?",
                         options={"official": "governments, institutions, standards",
                                  "paper": "scientific papers, preprints",
                                  "news": "journalism",
                                  "forum": "community discussions, Q&A",
                                  "other": "anything else"}),
            DecisionSpec(name="quality", kind=DecisionKind.SCORE,
                         question="Overall reliability of this source for the question?",
                         scale=["low", "medium", "high"]),
        ]
        for doc in pending:
            state = f"QUESTION: {question}\n\nDOCUMENT ({doc.final_url or doc.url}):\n" \
                    f"{wrap_external(doc.text[:4000])}"
            results = await decisions.decide_many(specs, state,
                                                       subject_kind="source",
                                                       subject_id=doc.source_id)
            relevant = bool(results[0].value) if results else True
            source_type = str(results[1].value) if results and results[1].value else None
            quality = int(results[2].value) if results and results[2].value is not None else None
            self.storage.update_source_evaluation(doc.source_id, source_type,
                                                  quality, results[0].confidence
                                                  if results else None, relevant)
            if not relevant:
                self.emit(ctx, "source_rejected", cycle, source_id=doc.source_id,
                          reason="not relevant")
                continue
            if quality == 0:
                self.emit(ctx, "source_rejected", cycle, source_id=doc.source_id,
                          reason="low quality")
                continue
            self.emit(ctx, "source_evaluated", cycle, source_id=doc.source_id,
                      source_type=source_type, quality=quality,
                      relevance=results[0].confidence if results else None)
            accepted.append(doc)
        return accepted

    async def _read_docs(self, ctx, question, cycle, accepted_docs, subquestions,
                         shared_state: SearchState | None = None,
                         budget_box: list[int] | None = None):
        """P1-6: relevance-oriented reading.

        - chunk selection: keyword overlap with the OPEN subquestions picks
          which chunks of each doc are read first (position no longer hides a
          bottom-of-page answer); ties keep document order.
        - subquestion assignment: best-matching subquestion per chunk (keyword
          overlap), not round-robin; a chunk may target the same subquestion
          from different docs.
        - budget: per-source share = ceil(remaining / remaining docs), so early
          docs cannot eat the whole cycle budget.
        - B6: contatori LIVE — ctx.stats["evidences"] cresce a ogni chunk e
          evidence_extracted esce per SINGOLO documento (count parziale del
          doc, total cumulativo del run), non una sola volta a fine ciclo.
        - B6: ogni doc letto viene registrato in shared_state.read_sources con
          le sotto-domande usate, cosi' i cicli successivi possono riusare i
          chunk da storage senza ri-leggere la fonte.
        - REGISTRO DI CICLO: lettura a piccoli gruppi (_READ_GROUP_DOCS) con
          rivalutazione del contributo tra gruppi (un gruppo con 0 evidenze
          viene diagnosticato via log.warning) e metriche di rendimento per
          fonte (content_len, passages_read, evidences_from, full_or_partial)
          accumulate in ctx.cycle_acc["reading"] per il cycle_registry.
        """
        evidences: list = []
        per_source_cap = max(1, self.settings.reader_chunks_per_source)
        remaining_docs = len(accepted_docs)
        budget = budget_box if budget_box is not None else [_MAX_EVIDENCES_PER_CYCLE]
        # registro di ciclo: metriche di rendimento per fonte letta
        acc = ctx.cycle_acc if ctx is not None else {}
        reading_metrics = acc.setdefault("reading", [])
        docs_with_ev = acc.setdefault("docs_with_evidence", set())

        def _kw(text: str) -> set[str]:
            stop = {"della", "dello", "delle", "degli", "nella", "nel", "per",
                    "con", "che", "come", "sono", "essere", "quale", "quali",
                    "the", "and", "for", "with", "that", "this", "from"}
            return {w for w in re.findall(r"[\wà-ù]{4,}", text.lower())
                    if w not in stop}

        sq_tokens = [(sq, _kw(sq.text)) for sq in subquestions]

        def _best_sq(ch: Chunk):
            """Subquestion with the highest keyword overlap with the chunk."""
            ch_tokens = _kw(ch.text)
            best, best_score = None, 0
            for sq, toks in sq_tokens:
                score = len(ch_tokens & toks)
                if score > best_score:
                    best, best_score = sq, score
            return best

        def _pick_chunks(doc) -> list[Chunk]:
            """The most subquestion-relevant chunks of the doc (stable ties)."""
            all_chunks = _chunkify(doc)
            if len(all_chunks) <= per_source_cap:
                return all_chunks
            scored = []
            for i, ch in enumerate(all_chunks):
                toks = _kw(ch.text)
                score = max((len(toks & stoks) for _sq, stoks in sq_tokens),
                            default=0)
                scored.append((-score, i))  # sort key: relevance desc, then pos
            scored.sort()
            picked = [all_chunks[i] for _s, i in scored[:per_source_cap]]
            picked.sort(key=lambda c: c.idx)  # keep reading order
            return picked

        # Budget a GRUPPI piccoli: la condivisione per-fonte resta quella per
        # documento (ceil(residuo / doc rimanenti)), ma tra un gruppo e l'altro
        # il contributo viene rivalutato e un gruppo muto viene diagnosticato.
        for g_start in range(0, len(accepted_docs), _READ_GROUP_DOCS):
            group = accepted_docs[g_start:g_start + _READ_GROUP_DOCS]
            ev_at_group_start = len(evidences)
            budget_at_group_start = budget[0]
            for doc in group:
                if budget[0] <= 0:
                    break
                share = max(1, -(-budget[0] // max(1, remaining_docs)))  # ceil division
                chunks = _pick_chunks(doc)[:max(1, min(per_source_cap, share))]
                remaining_docs -= 1
                doc_ev = 0          # evidenze di QUESTO documento (count parziale)
                passages = 0        # chunk effettivamente letti del documento
                used_sqs: set[str] = set()
                for ch in chunks:
                    if budget[0] <= 0:
                        break
                    passages += 1
                    sq = _best_sq(ch) if sq_tokens else (
                        subquestions[0] if subquestions else None)
                    self.emit(ctx, "source_reading", cycle, url=doc.final_url or doc.url,
                              subquestion=sq.text if sq else "", reused=False)
                    found = await self.reader.extract_evidences(
                        question, sq.text if sq else "", ch,
                        max_evidences=min(4, budget[0]),
                        subquestion_id=sq.subquestion_id if sq else None)
                    self.storage.add_chunk(ch)
                    if sq is not None:
                        used_sqs.add(sq.subquestion_id)
                    for e in found:
                        e.run_id = ctx.run_id
                        self.storage.add_evidence(e)
                        evidences.append(e)
                        doc_ev += 1
                        budget[0] -= 1
                    # B6 live: il contatore cresce DURANTE la lettura, non a fine ciclo
                    ctx.stats["evidences"] += len(found)
                    self._maybe_emit_usage(ctx)
                # B6 live: evento per SINGOLO documento (count parziale, total cumulativo)
                self.emit(ctx, "evidence_extracted", cycle, count=doc_ev,
                          total=ctx.stats["evidences"])
                # registro: rendimento della lettura di QUESTA fonte
                if doc_ev:
                    docs_with_ev.add(doc.source_id)
                reading_metrics.append({
                    "source_id": doc.source_id,
                    "url": doc.final_url or doc.url,
                    "content_len": len(doc.text),
                    "passages_read": passages,
                    "evidences_from": doc_ev,
                    "full_or_partial": (
                        "PARTIAL_CONTENT"
                        if doc.fetch_status.value == "PARTIAL_CONTENT"
                        else "SUCCESS"),
                    "duplicate": doc.source_id in acc.get("duplicate_ids", set()),
                })
                # B6: registra la fonte come gia' letta (per il riuso nei cicli dopo)
                if shared_state is not None:
                    shared_state.read_sources[doc.source_id] = {
                        "url": doc.url, "final_url": doc.final_url or doc.url,
                        "title": doc.title, "domain": doc.domain, "cycle": cycle}
                    if used_sqs:
                        (shared_state.used_sq_by_source.setdefault(doc.source_id, set())
                         .update(used_sqs))
            # rivalutazione del contributo del gruppo (diagnosticabilita')
            group_ev = len(evidences) - ev_at_group_start
            if group_ev == 0 and group:
                if budget_at_group_start <= 0 or budget[0] <= 0:
                    log.warning(
                        "[LETTURA] ciclo %s: gruppo doc %d-%d non letto, "
                        "budget evidenze esaurito (0 rimanenti)", cycle,
                        g_start + 1, g_start + len(group))
                else:
                    log.warning(
                        "[LETTURA] ciclo %s: gruppo doc %d-%d prodotto 0 "
                        "evidenze su %d documenti letti (possibile selezione "
                        "chunk poco rilevante): %s", cycle, g_start + 1,
                        g_start + len(group), len(group),
                        [d.source_id for d in group])
        # registro: documenti accettati ma non letti (budget esaurito)
        acc["unread_for_budget"] = max(0, len(accepted_docs) - len(reading_metrics))
        return evidences

    async def _reuse_chunks(self, ctx, question, cycle, open_sqs,
                            shared_state: SearchState,
                            budget_box: list[int]) -> list:
        """B6: riuso — nuove evidenze da chunk GIA' in storage per fonti lette
        in cicli precedenti, usando SOLO la sotto-domanda ora aperta come angolo.

        Zero re-download: il chunk arriva da storage.chunks_for_source (non si
        ri-scarica la pagina, non si ri-chunkifica il testo gia' letto); il
        reader viene chiamato solo per l'estrazione mirata col nuovo angolo.
        Lo stesso angolo non gira due volte sulla stessa fonte.
        """
        out: list = []
        if not open_sqs or budget_box[0] <= 0:
            return out
        for source_id, meta in list(shared_state.read_sources.items()):
            if budget_box[0] <= 0:
                break
            if meta.get("cycle", cycle) >= cycle:
                continue  # letta in QUESTO ciclo: gia' passata da _read_docs
            used = shared_state.used_sq_by_source.setdefault(source_id, set())
            pending = [sq for sq in open_sqs if sq.subquestion_id not in used]
            if not pending:
                continue
            rows = self.storage.chunks_for_source(source_id)
            if not rows:
                continue
            # l'angolo e' ora "consumato" per questa fonte, anche se il reader
            # non trovera' nulla: niente ri-estrazioni identiche al prossimo ciclo
            used.update(sq.subquestion_id for sq in pending)
            for sq in pending:
                for row in rows:
                    if budget_box[0] <= 0:
                        return out
                    ch = Chunk(source_id=source_id, chunk_id=row["id"],
                               idx=row["idx"], text=row["text"],
                               char_start=row["char_start"],
                               char_end=row["char_end"])
                    self.emit(ctx, "source_reading", cycle,
                              url=meta.get("final_url") or meta.get("url", ""),
                              subquestion=sq.text, reused=True)
                    found = await self.reader.extract_evidences(
                        question, sq.text, ch,
                        max_evidences=min(4, budget_box[0]),
                        subquestion_id=sq.subquestion_id)
                    for e in found:
                        e.run_id = ctx.run_id
                        self.storage.add_evidence(e)
                        out.append(e)
                        budget_box[0] -= 1
                    # B6 live: stessi contatori incrementali del read normale
                    ctx.stats["evidences"] += len(found)
                    self.emit(ctx, "evidence_extracted", cycle, count=len(found),
                              total=ctx.stats["evidences"], reused=True)
                    self._maybe_emit_usage(ctx)
        return out

    def _record_cycle_contribution(self, ctx, cycle: int, ev_start: int,
                                   domains_before: set[str],
                                   domains_now: set[str]) -> dict:
        """B6: contributo del ciclo in ctx.stats["per_cycle"] + evento
        cycle_contribution (la UI lo ignorera' per ora; serve nel DB/events)."""
        entry = {
            "new_evidences": max(0, ctx.stats["evidences"] - ev_start),
            "new_domains": len({d for d in domains_now
                                if d and d not in domains_before}),
        }
        ctx.stats.setdefault("per_cycle", {})[cycle] = entry
        self.emit(ctx, "cycle_contribution", cycle, **entry)
        return entry

    # ---------- registro di ciclo ----------
    def _pages_limit_origin(self, acc: dict) -> str:
        """Diagnosi DERIVATA dai numeri reali del ciclo: da dove nasce il
        limite di pagine lette (cap query×pagine? risultati disponibili?
        deduplicati? errori? cache?)."""
        queries = acc.get("queries", ())
        q_exec = len(queries)
        p_cap = self.settings.max_pages_per_query
        q_cap = self.settings.max_queries_per_cycle
        raw = acc.get("results_found", 0)
        cand = acc.get("candidates", 0)
        dups = acc.get("duplicates", 0)
        errs = acc.get("fetch_errors", 0)
        cache = acc.get("reused_cache", 0)
        acquired = acc.get("acquired_new", 0)
        cap = q_exec * p_cap
        if q_exec == 0:
            return ("nessuna query eseguita nel ciclo (tutte gia' viste o cap "
                    f"max_queries_per_cycle={q_cap} esaurito): nessuna pagina "
                    "acquisibile")
        if raw == 0:
            return (f"0 risultati dal provider su {q_exec} query: nessun "
                    f"candidato (cap teorico {cap} = {q_exec} query × "
                    f"{p_cap} pagine/query)")
        parts = [f"cap teorico {cap} = {q_exec} query eseguite × "
                 f"max_pages_per_query={p_cap} (max_queries_per_cycle={q_cap})"]
        if raw < cap:
            parts.append(f"il provider ha restituito {raw} risultati (< cap): "
                         "disponibile meno del massimo teorico")
        dropped = raw - cand
        if dropped > 0:
            parts.append(f"{dropped} scartati perche' URL gia' visti in cicli "
                         "precedenti (deduplicati tra cicli)")
        if errs:
            parts.append(f"{errs} errori di fetch")
        if dups:
            parts.append(f"{dups} duplicati di contenuto scartati "
                         "(stesso content_hash)")
        if cache:
            parts.append(f"{cache} serviti dalla cache disco (nessun ri-download)")
        parts.append(f"→ {acquired} nuove fonti acquisite")
        return "; ".join(parts)

    def _emit_cycle_registry(self, ctx, cycle: int,
                             shared_state: SearchState | None = None,
                             gap_status: dict | None = None,
                             early: str = "") -> dict:
        """Fine ciclo: REGISTRO CONSULTABILE — emesso come evento
        cycle_registry e conservato in ctx.stats["cycles_registry"] (poi
        persistito in runs.limits_json sotto la chiave "cycles").

        Fonti dei dati: accumulatori del ciclo (ctx.cycle_acc: query, risultati,
        fetch, dedup), SearchState, seen_content per ciclo (ctx.hashes_by_cycle).
        """
        acc = ctx.cycle_acc or {}
        hashes_now = ctx.hashes_by_cycle.get(cycle, set())
        prev_hashes: set[str] = set()
        for c, hs in ctx.hashes_by_cycle.items():
            if c < cycle:
                prev_hashes |= hs
        payload = {
            "cycle": cycle,
            "role": cycle_role(cycle),
            "queries_executed": list(acc.get("queries", ())),
            "results_found": acc.get("results_found", 0),
            "candidates": acc.get("candidates", 0),
            "acquired_new": acc.get("acquired_new", 0),
            "reused_cache": acc.get("reused_cache", 0),
            "reused_evidences": acc.get("reused_evidences", 0),
            "reread_updated": acc.get("reread_updated", 0),
            "reread_motivo": acc.get("reread_motivo", ""),
            "duplicates": acc.get("duplicates", 0),
            "fetch_errors": acc.get("fetch_errors", 0),
            "docs_with_evidence": len(acc.get("docs_with_evidence", set())),
            "new_vs_previous": len(hashes_now - prev_hashes),
            "unread_for_budget": acc.get("unread_for_budget", 0),
            "reading": list(acc.get("reading", ())),
            "pages_limit_origin": self._pages_limit_origin(acc),
            "gap_status": dict(gap_status) if gap_status else None,
        }
        if early:
            payload["terminated_early"] = early
        # il payload porta gia' "cycle": non ripeterlo come argomento posizionale
        event_payload = {k: v for k, v in payload.items() if k != "cycle"}
        self.emit(ctx, "cycle_registry", cycle, **event_payload)
        ctx.stats.setdefault("cycles_registry", {})[str(cycle)] = payload
        ctx.cycle_acc = {}
        return payload

    def _maybe_emit_usage(self, ctx) -> None:
        """Throttled usage_update so the UI shows live counters."""
        now = time.monotonic()
        if now - ctx.last_usage_emit < self.settings.usage_emit_every_s:
            return
        ctx.last_usage_emit = now
        self.emit(ctx, "usage_update", ctx.cycle,
                  llm_calls=getattr(self.llm, "llm_calls", 0),
                  tokens_in=getattr(self.llm, "tokens_in", 0),
                  tokens_out=getattr(self.llm, "tokens_out", 0),
                  sources=ctx.stats["sources"],
                  evidences=ctx.stats["evidences"])

    # ---------- export ----------
    def _export_json(self, run_id, question, path, report_md, claims, evidences):
        sources = self.storage.sources_for_run(run_id)
        payload = {
            "question": question,
            "report_markdown": report_md,
            "claims": [c.model_dump() for c in claims],
            "evidences": [e.model_dump() for e in evidences],
            "sources": sources,
            "verifications": self.storage.verifications_for_run(run_id),
            "decisions": self.storage.decisions_for_run(run_id),
            "usage": self.storage.usage_for_run(run_id),
            "provenance": ProvenanceTracker(self.storage, run_id).export_graph(),
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")


# ---------- module helpers ----------
_SUPPORT_RE = re.compile(r"\[support:(supported|contradicted|insufficient|mixed)\]")


def _count_support(claims) -> dict[str, int]:
    """Count the [support:X] prefixes written by the verifier (B12).
    Claims without the prefix are not counted (older runs / non-LLM paths)."""
    out = {"supported": 0, "contradicted": 0, "insufficient": 0, "mixed": 0}
    for c in claims:
        m = _SUPPORT_RE.search(getattr(c, "verdict_reason", "") or "")
        if m:
            out[m.group(1)] += 1
    return out


def _as_query(run_id: str, subquestion_id: str, text: str, cycle: int, idx: int):
    from app.models import GeneratedQuery
    return GeneratedQuery(query_id=f"{run_id[:24]}_qc{cycle}_{idx}",
                          subquestion_id=subquestion_id, text=text, cycle=cycle)


def _chunkify(doc):
    from app.extraction.chunking import chunk_text
    out = []
    for i, (start, end, text) in enumerate(
            chunk_text(doc.text, max_chars=4000, overlap=200)):
        out.append(Chunk(chunk_id=f"chk_{doc.source_id}_{i}", source_id=doc.source_id,
                         idx=i, text=text, char_start=start, char_end=end))
    return out


def _final_markdown(title: str, body: str, claims, evidences, src_map) -> str:
    """Bibliography off the UNIFIED citation numbers (P0-2): claim n -> its
    evidences' sources; a source keeps the number of its first citing claim,
    additional claims citing it reuse that number in their Fonti annotation.
    """
    # single H1: title added ONCE here; body must not carry its own
    body = re.sub(r"^\s*#\s+[^\n]+\n+", "", body)
    lines = [f"# {title}", "", body.strip(), "", "## Fonti", ""]
    seen_src: dict[str, int] = {}  # source_id -> bibliography number
    src_no: list[tuple[int, str]] = []  # (bib_no, line)
    for c in sorted((c for c in claims if c.final_citation_no),
                    key=lambda c: c.final_citation_no):
        for eid in c.evidence_ids:
            e = next((x for x in evidences if x.evidence_id == eid), None)
            if e is None or e.source_id in seen_src:
                continue
            src = src_map.get(e.source_id)
            if src is None:
                continue
            seen_src[e.source_id] = c.final_citation_no
            date = f", {src.published_at}" if src.published_at else ""
            src_no.append((c.final_citation_no,
                           f"{src.title or src.domain} — {src.final_url or src.url}"
                           f" ({src.domain}{date})"))
    # bibliography ordered by number: a source cited first by claim 3 takes 3
    for n, ln in sorted(src_no, key=lambda t: t[0]):
        lines.append(f"{n}. {ln}")
    return "\n".join(lines)
