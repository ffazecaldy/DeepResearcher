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
from app.agent.search_state import SearchState
from app.events.bus import EventBus
from app.limits import LimitReached, RuntimeBudget
from app.models import (Chunk, DecisionKind, DecisionSpec, Event, RunOutcome,
                        SubQuestion)
from app.provenance.tracker import ProvenanceTracker
from app.security import wrap_external  # noqa: F401  (re-exported for tests)

log = logging.getLogger(__name__)

_DEPTH_SUBQ = {Depth.RAPIDA: 3, Depth.STANDARD: 4, Depth.APPROFONDITA: 6}
_MAX_EVIDENCES_PER_CYCLE = 60


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

    def _snowball_queries(self, cycle: int, seen_urls: set[str]):
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
                query_id=f"{self.run_id[:24]}_sb{cycle}_{len(out) + 1}",
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
                from app.agent.search_state import cycle_role as _cycle_role
                self.emit(ctx, "cycle_started", cycle, role=_cycle_role(cycle))
                # B5: deadline di ciclo + snapshot token a inizio ciclo
                cycle_deadline[0] = (time.monotonic() + per_cycle_s
                                     if per_cycle_s > 0 else 0.0)
                cycle_tokens_start[0] = (getattr(self.llm, "tokens_in", 0)
                                         + getattr(self.llm, "tokens_out", 0))
                ctx.cycle_tokens_in = 0
                ctx.cycle_tokens_out = 0

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
                    all_q = (gen_queries
                             + [_as_query(self.run_id, q.subquestion_id, q.text,
                                          cycle, i)
                                for i, q in enumerate(gap_new_queries)]
                             + ctx.snowball_queries(cycle, seen_urls))
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
                self.emit(ctx, "query_started", cycle, count=len(queries))
                items = await self.searcher.run_queries(
                    queries, self.settings.max_pages_per_query)
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
                            ctx.stats["sources"] += 1
                            self.emit(ctx, "source_fetched", cycle, source_id=doc.source_id,
                                      url=doc.final_url or doc.url)
                        else:
                            self.emit(ctx, "source_failed", cycle, source_id=doc.source_id,
                                      url=doc.url, error=doc.error)
                    accepted = await self._evaluate_docs(run_id, cycle, docs, question, decisions, ctx)
                    # B4: snowballing — link citati dalle pagine accettate
                    from app.agent.snowball import extract_links as _xlinks
                    for doc in accepted:
                        for u in _xlinks(doc.text[:8000],
                                         base_url=doc.final_url or doc.url,
                                         max_links=10):
                            ctx.snowball.propose(u)
                    t_fetch = time.perf_counter() - _t_fetch
                    ctx.timings.append({"cycle": cycle, "step": "fetch+judge",
                                          "s": round(t_fetch, 1)})
                    log.debug("[TEMPI] ciclo %s fetch+judge=%.1fs", cycle, t_fetch)
                    accepted_docs.extend(accepted)
                    _t_read = time.perf_counter()
                    new_evs = await self._read_docs(ctx, question, cycle, accepted, subquestions)
                    evidences.extend(new_evs)
                    ctx.stats["evidences"] += len(new_evs)
                    ctx.timings.append({"cycle": cycle, "step": "read(LLM)",
                                          "s": round(time.perf_counter() - _t_read, 1)})
                    log.debug("[TEMPI] ciclo %s read=%.1fs", cycle,
                              time.perf_counter() - _t_read)
                    self.emit(ctx, "evidence_extracted", cycle, count=len(new_evs),
                              total=len(evidences))
                    self._maybe_emit_usage(ctx)
                    # B5: check deadline per-ciclo (subito dopo il primo step pesante)
                    if cycle_deadline[0] and time.monotonic() > cycle_deadline[0]:
                        limit_note = (limit_note or
                                      f"max_seconds_per_cycle={per_cycle_s} al ciclo {cycle}")
                        stop_reason, stop_detail = "LIMITE_CICLO", (
                            f"budget per ciclo esaurito: {per_cycle_s}s al ciclo {cycle}")
                        log.debug("[TERMINAZIONE] %s — %s", stop_reason, stop_detail)
                        self.emit(ctx, "cycle_completed", cycle, complete=True,
                                  stop_reason=stop_reason, stop_detail=stop_detail)
                        break

                    # P1-9: cooperative deadline — also checked between fetch
                    # and read (a long read must not start past the deadline),
                    # and writer/verification get a RESERVED tail of the budget
                    if cancel.is_set():
                        status = "cancelled"
                        stop_reason, stop_detail = "ANNULLATO", "richiesta utente"
                        break
                    if cycle_deadline[0] and time.monotonic() > cycle_deadline[0]:
                        limit_note = (limit_note or
                                      f"max_seconds_per_cycle={per_cycle_s} al ciclo {cycle}")
                        stop_reason, stop_detail = "LIMITE_CICLO", (
                            f"budget per ciclo esaurito: {per_cycle_s}s al ciclo {cycle}")
                        log.debug("[TERMINAZIONE] %s — %s", stop_reason, stop_detail)
                        self.emit(ctx, "cycle_completed", cycle, complete=True,
                                  stop_reason=stop_reason, stop_detail=stop_detail)
                        break

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
                # P0-1: the FINAL text is the verified one — persisted, exported,
                # streamed. Draft corrections can no longer be thrown away.
                report_md = _final_markdown(draft.title, verified.markdown,
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
            doc.published_at, doc.author, doc.redirects, doc.error, doc.byte_len)

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

    async def _read_docs(self, ctx, question, cycle, accepted_docs, subquestions):
        """P1-6: relevance-oriented reading.

        - chunk selection: keyword overlap with the OPEN subquestions picks
          which chunks of each doc are read first (position no longer hides a
          bottom-of-page answer); ties keep document order.
        - subquestion assignment: best-matching subquestion per chunk (keyword
          overlap), not round-robin; a chunk may target the same subquestion
          from different docs.
        - budget: per-source share = ceil(remaining / remaining docs), so early
          docs cannot eat the whole cycle budget.
        """
        evidences = []
        total_budget = _MAX_EVIDENCES_PER_CYCLE
        per_source_cap = max(1, self.settings.reader_chunks_per_source)
        remaining_docs = len(accepted_docs)
        budget = total_budget

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

        for doc in accepted_docs:
            if budget <= 0:
                break
            share = max(1, -(-budget // max(1, remaining_docs)))  # ceil division
            chunks = _pick_chunks(doc)[:max(1, min(per_source_cap, share))]
            remaining_docs -= 1
            for ch in chunks:
                if budget <= 0:
                    break
                sq = _best_sq(ch) if sq_tokens else (
                    subquestions[0] if subquestions else None)
                self.emit(ctx, "source_reading", cycle, url=doc.final_url or doc.url,
                          subquestion=sq.text if sq else "")
                found = await self.reader.extract_evidences(
                    question, sq.text if sq else "", ch,
                    max_evidences=min(4, budget),
                    subquestion_id=sq.subquestion_id if sq else None)
                self.storage.add_chunk(ch)
                for e in found:
                    e.run_id = ctx.run_id
                    self.storage.add_evidence(e)
                    evidences.append(e)
                    budget -= 1
        return evidences

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
