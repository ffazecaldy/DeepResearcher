"""Orchestrator: iterative research pipeline (plan→search→fetch→decide→read→gap→write→verify)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from app.config import Depth
from app.events.bus import EventBus
from app.limits import LimitReached, RuntimeBudget
from app.models import (Chunk, DecisionKind, DecisionSpec, Event, RunOutcome,
                        SubQuestion)
from app.provenance.tracker import ProvenanceTracker
from app.security import wrap_external  # noqa: F401  (re-exported for tests)

log = logging.getLogger(__name__)

_DEPTH_SUBQ = {Depth.RAPIDA: 3, Depth.STANDARD: 4, Depth.APPROFONDITA: 6}
_MAX_EVIDENCES_PER_CYCLE = 12


def _unwrap(x):
    """Accept either the object or a (object, closer) tuple from bootstrap."""
    return x[0] if isinstance(x, tuple) else x


class Orchestrator:
    def __init__(self, settings, storage, bus: EventBus, llm,
                 searcher, fetcher, decisions, planner, reader,
                 gap_checker, writer, verifier):
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
        self.run_id: str = ""

    # ---------- events ----------
    def emit(self, type_: str, cycle: int, **payload) -> None:
        self.bus.publish(Event(type=type_, run_id=self.run_id, cycle=cycle,
                               payload=payload))

    # ---------- run ----------
    async def run(self, question: str, language: str, depth: Depth, run_id: str,
                  cancel: asyncio.Event) -> RunOutcome:
        self.run_id = run_id
        self.decisions.run_id = run_id
        self.storage.usage_init(run_id)
        self.storage.create_run(run_id, question, language, depth.value)
        self.emit("run_started", 0, question=question, depth=depth.value,
                  language=language)
        budget = RuntimeBudget(max_runtime_seconds=self.settings.max_runtime_seconds,
                               max_total_pages=self.settings.max_total_pages)
        status = "completed"
        error: str | None = None
        limit_note = ""
        plan = None
        evidences = []
        subquestions: list[SubQuestion] = []
        accepted_docs = []
        report_md = ""
        report_claims = []

        try:
            for cycle in range(1, self.settings.cycle_budget(depth) + 1):
                if cancel.is_set():
                    status = "cancelled"
                    break
                try:
                    budget.check_time()
                except LimitReached as exc:
                    limit_note = str(exc)
                    break
                self.emit("cycle_started", cycle)

                # ---- planning (first cycle only) ----
                if plan is None:
                    self.emit("planning_started", cycle)
                    plan = await self.planner.make_plan(
                        question, language,
                        max_subquestions=_DEPTH_SUBQ[depth])
                    subquestions = plan.subquestions
                    for sq in subquestions:
                        self.storage.add_subquestion(run_id, sq.subquestion_id,
                                                     sq.idx, sq.text)
                    cap = self.settings.max_queries_per_cycle
                    for q in plan.queries[:cap]:
                        self.storage.add_query(run_id, q.query_id,
                                               q.subquestion_id, q.text, 1,
                                               self.settings.search_provider.value)
                    self.emit("plan_generated", cycle, title=plan.title,
                              subquestions=[s.text for s in subquestions],
                              queries=[q.text for q in plan.queries[:cap]])

                # ---- search ----
                queries = plan.queries[:self.settings.max_queries_per_cycle]
                self.emit("query_started", cycle, count=len(queries))
                items = await self.searcher.run_queries(
                    queries, self.settings.max_pages_per_query)
                self.emit("search_result_found", cycle, count=len(items))
                if items:
                    try:
                        docs = await self.fetcher.fetch_all(items, budget)
                    except LimitReached as exc:
                        docs = []
                        limit_note = str(exc)
                    for doc in docs:
                        self._persist_source(run_id, doc)
                        if doc.fetch_status.value.startswith(("SUCCESS", "PARTIAL")):
                            self.emit("source_fetched", cycle, source_id=doc.source_id,
                                      url=doc.final_url or doc.url)
                        else:
                            self.emit("source_failed", cycle, source_id=doc.source_id,
                                      url=doc.url, error=doc.error)
                    accepted = await self._evaluate_docs(run_id, cycle, docs, question)
                    accepted_docs.extend(accepted)
                    new_evs = await self._read_docs(question, cycle, accepted, subquestions)
                    evidences.extend(new_evs)
                    self.emit("evidence_extracted", cycle, count=len(new_evs),
                              total=len(evidences))

                if cancel.is_set():
                    status = "cancelled"
                    break

                # ---- gap check ----
                if depth != Depth.RAPIDA or cycle < self.settings.cycle_budget(depth):
                    self.emit("gap_check_started", cycle)
                    report = await self.gap_checker.check(question, subquestions,
                                                          evidences)
                    if report.complete:
                        self.emit("cycle_completed", cycle, complete=True)
                        break
                    self.emit("gap_detected", cycle,
                              missing=report.missing_subquestions,
                              contradictions=report.contradictions,
                              new_queries=[q.model_dump() for q in report.new_queries])
                    if report.new_queries:
                        plan.queries = [
                            _as_query(q.subquestion_id, q.text, cycle + 1)
                            for q in report.new_queries
                        ][:self.settings.max_queries_per_cycle]
                self.emit("cycle_completed", cycle)

            # ---- writing ----
            if evidences and not cancel.is_set():
                self.emit("writing_started", 0)
                draft = await self.writer.write(question, language, subquestions,
                                                evidences, limit_note=limit_note)
                for c in draft.claims:
                    self.storage.add_claim(run_id, c.claim_id, c.text,
                                           c.citation_no, c.evidence_ids)
                self.emit("report_chunk", 0, title=draft.title,
                          markdown=draft.markdown)

                # ---- verification ----
                self.emit("verification_started", 0)
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
                    self.emit(ev_name, 0, claim_id=c.claim_id, reason=c.verdict_reason)
                report_md = _final_markdown(draft.title, draft.markdown,
                                            verified.claims, evidences, src_map)
                report_claims = verified.claims
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
            self.emit("run_failed", 0, error=error)

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
        self.storage.finish_run(run_id, status, error,
                                bool(limit_note), limit_note)
        self.emit("run_completed" if status == "completed" else
                  ("run_cancelled" if status == "cancelled" else "run_failed"),
                  0, status=status, limit_note=limit_note)
        stats = {
            "sources": len(accepted_docs),
            "evidences": len(evidences),
            "claims": len(report_claims),
            "decisions": getattr(self.decisions, "counts", {}),
        }
        usage = self.storage.usage_for_run(run_id) or {}
        stats["llm_calls"] = getattr(self.llm, "llm_calls", 0)
        stats["llm_tokens_in"] = getattr(self.llm, "tokens_in", 0)
        stats["llm_tokens_out"] = getattr(self.llm, "tokens_out", 0)
        stats["usage_db"] = usage
        return RunOutcome(run_id=run_id, status=status, question=question,
                          language=language, markdown_path=md_path,
                          json_path=json_path, limit_reached=bool(limit_note),
                          limit_note=limit_note, stats=stats)

    # ---------- steps ----------
    def _persist_source(self, run_id: str, doc) -> None:
        self.storage.add_source(
            doc.source_id, run_id, doc.url, doc.final_url, doc.domain, doc.title,
            doc.http_status, doc.content_type, doc.fetch_status.value,
            doc.published_at, doc.author, doc.redirects, doc.error, doc.byte_len)

    async def _evaluate_docs(self, run_id, cycle, docs, question):
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
            results = await self.decisions.decide_many(specs, state,
                                                       subject_kind="source",
                                                       subject_id=doc.source_id)
            relevant = bool(results[0].value) if results else True
            source_type = str(results[1].value) if results and results[1].value else None
            quality = int(results[2].value) if results and results[2].value is not None else None
            self.storage.update_source_evaluation(doc.source_id, source_type,
                                                  quality, results[0].confidence
                                                  if results else None, relevant)
            if not relevant:
                self.emit("source_rejected", cycle, source_id=doc.source_id,
                          reason="not relevant")
                continue
            if quality == 0:
                self.emit("source_rejected", cycle, source_id=doc.source_id,
                          reason="low quality")
                continue
            self.emit("source_evaluated", cycle, source_id=doc.source_id,
                      source_type=source_type, quality=quality,
                      relevance=results[0].confidence if results else None)
            accepted.append(doc)
        return accepted

    async def _read_docs(self, question, cycle, accepted_docs, subquestions):
        evidences = []
        budget = _MAX_EVIDENCES_PER_CYCLE
        for doc in accepted_docs:
            if budget <= 0:
                break
            chunks = _chunkify(doc)
            for ch in chunks:
                if budget <= 0:
                    break
                sq = subquestions[0] if subquestions else None
                found = await self.reader.extract_evidences(
                    question, sq.text if sq else "", ch,
                    max_evidences=min(4, budget),
                    subquestion_id=sq.subquestion_id if sq else None)
                for e in found:
                    e.run_id = self.run_id
                    self.storage.add_chunk(ch)
                    self.storage.add_evidence(e)
                    evidences.append(e)
                    budget -= 1
        return evidences

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
def _as_query(subquestion_id: str, text: str, cycle: int):
    from app.models import GeneratedQuery
    return GeneratedQuery(query_id=f"q_c{cycle}_{abs(hash(text)) % 9999}",
                          subquestion_id=subquestion_id, text=text, cycle=cycle)


def _chunkify(doc):
    from app.extraction.chunking import chunk_text
    out = []
    for i, (start, end, text) in enumerate(
            chunk_text(doc.text, max_chars=4000, overlap=200)[:4]):
        out.append(Chunk(chunk_id=f"chk_{doc.source_id}_{i}", source_id=doc.source_id,
                         idx=i, text=text, char_start=start, char_end=end))
    return out


def _final_markdown(title: str, body: str, claims, evidences, src_map) -> str:
    lines = [f"# {title}", "", body.strip(), "", "## Fonti", ""]
    seen_src: dict[str, int] = {}
    for c in sorted(claims, key=lambda c: c.citation_no):
        for eid in c.evidence_ids:
            e = next((x for x in evidences if x.evidence_id == eid), None)
            if e is None or e.source_id in seen_src:
                continue
            src = src_map.get(e.source_id)
            if src is None:
                continue
            seen_src[e.source_id] = c.citation_no
            n = len(seen_src)
            date = f", {src.published_at}" if src.published_at else ""
            lines.append(f"{n}. {src.title or src.domain} — {src.final_url or src.url}"
                         f" ({src.domain}{date})")
    return "\n".join(lines)
