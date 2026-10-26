"""SQLite persistence: runs, queries, sources, evidences, decisions, reports, events."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from app.models import Chunk, DecisionResult, Event, Evidence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, question TEXT NOT NULL, language TEXT, depth TEXT,
  status TEXT NOT NULL, started_at REAL, ended_at REAL, limits_json TEXT DEFAULT '{}',
  error TEXT, limit_reached INTEGER DEFAULT 0, limit_note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS subquestions(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idx INTEGER, text TEXT
);
CREATE TABLE IF NOT EXISTS queries(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, subquestion_id TEXT, text TEXT,
  cycle INTEGER, provider TEXT, status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS search_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, query_id TEXT,
  url TEXT, url_normalized TEXT, title TEXT, position INTEGER
);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, url TEXT, url_final TEXT, domain TEXT,
  title TEXT, http_status INTEGER, content_type TEXT, fetch_status TEXT,
  published_at TEXT, author TEXT, redirects_json TEXT, error TEXT, byte_len INTEGER,
  content_hash TEXT DEFAULT '', source_type TEXT, quality_score INTEGER,
  relevance REAL, accepted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, idx INTEGER, text TEXT,
  char_start INTEGER, char_end INTEGER
);
CREATE TABLE IF NOT EXISTS evidences(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_id TEXT, chunk_id TEXT,
  quote TEXT, span_start INTEGER, span_end INTEGER, claim TEXT, subquestion_id TEXT,
  source_type TEXT, quality_score INTEGER, confidence REAL
);
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, decision_type TEXT, engine TEXT,
  subject_kind TEXT, subject_id TEXT, value_json TEXT, confidence REAL,
  abstained INTEGER DEFAULT 0, latency_ms REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS claims(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, evidence_ids_json TEXT, text TEXT,
  citation_no INTEGER, final_citation_no INTEGER
);
CREATE TABLE IF NOT EXISTS reports(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, title TEXT, markdown TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, claim_id TEXT,
  verdict TEXT, reason TEXT, corrected_text TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts REAL, type TEXT,
  cycle INTEGER, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS usage(
  run_id TEXT PRIMARY KEY, llm_calls INTEGER DEFAULT 0, llm_tokens_in INTEGER DEFAULT 0,
  llm_tokens_out INTEGER DEFAULT 0, laya_decisions INTEGER DEFAULT 0,
  llm_decisions INTEGER DEFAULT 0
);
"""

_TABLES_WITH_RUN = (
    "runs", "subquestions", "queries", "search_results", "sources", "chunks",
    "evidences", "decisions", "claims", "reports", "verifications", "events", "usage",
)


class Storage:
    """Thread-safe SQLite DAO (a single connection guarded by a lock)."""

    def __init__(self, db_path: str | Path):
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent migrations for DBs created by older versions (the
        CREATE TABLE IF NOT EXISTS never alters existing tables)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(claims)")}
        if "final_citation_no" not in cols:
            self._conn.execute(
                "ALTER TABLE claims ADD COLUMN final_citation_no INTEGER")
        src_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sources)")}
        if "content_hash" not in src_cols:
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN content_hash TEXT DEFAULT ''")
        # future migrations go here, each guarded by a PRAGMA table_info check

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # ---------- runs ----------
    def create_run(self, run_id: str, question: str, language: str, depth: str,
                   limits_json: str = "{}") -> None:
        self._exec(
            "INSERT INTO runs(id, question, language, depth, status, started_at, limits_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (run_id, question, language, depth, "running", time.time(), limits_json),
        )

    def finish_run(self, run_id: str, status: str, error: str | None = None,
                   limit_reached: bool = False, limit_note: str = "") -> None:
        self._exec(
            "UPDATE runs SET status=?, ended_at=?, error=?, limit_reached=?, limit_note=? WHERE id=?",
            (status, time.time(), error, int(limit_reached), limit_note, run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return rows[0] if rows else None

    def list_runs(self) -> list[dict[str, Any]]:
        return self._query(
            "SELECT id, question, language, depth, status, started_at, ended_at, limit_reached"
            " FROM runs ORDER BY started_at DESC"
        )

    def delete_run(self, run_id: str) -> None:
        for t in _TABLES_WITH_RUN:
            if t == "runs":
                self._exec("DELETE FROM runs WHERE id=?", (run_id,))
            elif t == "chunks":  # no run_id column: via sources
                self._exec(
                    "DELETE FROM chunks WHERE source_id IN"
                    " (SELECT id FROM sources WHERE run_id=?)", (run_id,))
            else:
                self._exec(f"DELETE FROM {t} WHERE run_id=?", (run_id,))

    def clear_all_runs(self) -> int:
        """Delete the whole history; returns the number of runs removed."""
        n = self._query("SELECT COUNT(*) AS c FROM runs")[0]["c"]
        for t in _TABLES_WITH_RUN:
            self._exec(f"DELETE FROM {t}")
        return int(n)

    # ---------- plan / queries ----------
    def add_subquestion(self, run_id: str, sid: str, idx: int, text: str) -> None:
        self._exec("INSERT INTO subquestions(id, run_id, idx, text) VALUES(?,?,?,?)",
                   (sid, run_id, idx, text))

    def add_query(self, run_id: str, qid: str, subquestion_id: str, text: str,
                  cycle: int, provider: str) -> None:
        self._exec(
            "INSERT INTO queries(id, run_id, subquestion_id, text, cycle, provider)"
            " VALUES(?,?,?,?,?,?)",
            (qid, run_id, subquestion_id, text, cycle, provider),
        )

    def set_query_status(self, qid: str, status: str) -> None:
        self._exec("UPDATE queries SET status=? WHERE id=?", (status, qid))

    def add_search_result(self, run_id: str, query_id: str, url: str, url_norm: str,
                          title: str, position: int) -> None:
        self._exec(
            "INSERT INTO search_results(run_id, query_id, url, url_normalized, title, position)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, query_id, url, url_norm, title, position),
        )

    # ---------- sources / chunks / evidences ----------
    def add_source(self, source_id: str, run_id: str, url: str, url_final: str,
                   domain: str, title: str, http_status: int | None, content_type: str,
                   fetch_status: str, published_at: str | None, author: str | None,
                   redirects: list[str], error: str | None, byte_len: int,
                   content_hash: str = "") -> None:
        self._exec(
            "INSERT INTO sources(id, run_id, url, url_final, domain, title, http_status,"
            " content_type, fetch_status, published_at, author, redirects_json, error,"
            " byte_len, content_hash)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, run_id, url, url_final, domain, title, http_status, content_type,
             fetch_status, published_at, author, json.dumps(redirects), error, byte_len,
             content_hash),
        )

    def content_hash_seen(self, run_id: str, content_hash: str) -> bool:
        """True se questo run ha gia' una fonte con lo stesso content_hash
        (stesso testo su URL diverso = non fonte indipendente)."""
        if not content_hash:
            return False
        rows = self._query(
            "SELECT 1 FROM sources WHERE run_id=? AND content_hash=? LIMIT 1",
            (run_id, content_hash),
        )
        return bool(rows)

    def update_source_evaluation(self, source_id: str, source_type: str | None,
                                 quality_score: int | None, relevance: float | None,
                                 accepted: bool) -> None:
        self._exec(
            "UPDATE sources SET source_type=?, quality_score=?, relevance=?, accepted=? WHERE id=?",
            (source_type, quality_score, relevance, int(accepted), source_id),
        )

    def add_chunk(self, chunk: Chunk) -> None:
        # idempotent: the same chunk may be produced by multiple read passes
        self._exec(
            "INSERT OR IGNORE INTO chunks(id, source_id, idx, text, char_start, char_end)"
            " VALUES(?,?,?,?,?,?)",
            (chunk.chunk_id, chunk.source_id, chunk.idx, chunk.text,
             chunk.char_start, chunk.char_end),
        )

    def add_evidence(self, e: Evidence) -> None:
        self._exec(
            "INSERT INTO evidences(id, run_id, source_id, chunk_id, quote, span_start,"
            " span_end, claim, subquestion_id, source_type, quality_score, confidence)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.evidence_id, e.run_id, e.source_id, e.chunk_id, e.quote, e.span_start,
             e.span_end, e.claim, e.subquestion_id, e.source_type, e.quality_score,
             e.confidence),
        )

    # ---------- decisions ----------
    def add_decision(self, run_id: str, decision_type: str, d: DecisionResult) -> None:
        self._exec(
            "INSERT INTO decisions(id, run_id, decision_type, engine, subject_kind,"
            " subject_id, value_json, confidence, abstained, latency_ms, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (d.decision_id, run_id, decision_type, d.engine, d.subject_kind, d.subject_id,
             json.dumps(d.value), d.confidence, int(d.abstained), d.latency_ms, time.time()),
        )

    # ---------- claims / report / verification ----------
    def add_claim(self, run_id: str, claim_id: str, text: str, citation_no: int,
                  evidence_ids: list[str], final_citation_no: int | None = None) -> None:
        self._exec(
            "INSERT INTO claims(id, run_id, evidence_ids_json, text, citation_no,"
            " final_citation_no) VALUES(?,?,?,?,?,?)",
            (claim_id, run_id, json.dumps(evidence_ids), text, citation_no,
             final_citation_no),
        )

    def save_report(self, run_id: str, report_id: str, title: str, markdown: str) -> None:
        self._exec(
            "INSERT INTO reports(id, run_id, title, markdown, created_at) VALUES(?,?,?,?,?)",
            (report_id, run_id, title, markdown, time.time()),
        )

    def add_verification(self, run_id: str, claim_id: str, verdict: str,
                         reason: str, corrected_text: str | None) -> None:
        self._exec(
            "INSERT INTO verifications(run_id, claim_id, verdict, reason, corrected_text)"
            " VALUES(?,?,?,?,?)",
            (run_id, claim_id, verdict, reason, corrected_text),
        )

    # ---------- events / usage ----------
    def add_event(self, event: Event) -> None:
        self._exec(
            "INSERT INTO events(run_id, ts, type, cycle, payload_json) VALUES(?,?,?,?,?)",
            (event.run_id, event.ts, event.type, event.cycle,
             json.dumps(event.payload, ensure_ascii=False)),
        )

    def usage_init(self, run_id: str) -> None:
        self._exec("INSERT OR IGNORE INTO usage(run_id) VALUES(?)", (run_id,))

    def usage_inc(self, run_id: str, **counters: int) -> None:
        allowed = {"llm_calls", "llm_tokens_in", "llm_tokens_out",
                   "laya_decisions", "llm_decisions"}
        for k, v in counters.items():
            if k not in allowed or not v:
                continue
            self._exec(f"UPDATE usage SET {k} = {k} + ? WHERE run_id=?", (v, run_id))

    # ---------- getters ----------
    def _rows(self, table: str, run_id: str, order: str = "rowid") -> list[dict[str, Any]]:
        return self._query(f"SELECT * FROM {table} WHERE run_id=? ORDER BY {order}", (run_id,))

    def subquestions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("subquestions", run_id, "idx")

    def queries_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("queries", run_id, "rowid")

    def search_results_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("search_results", run_id, "id")

    def sources_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("sources", run_id, "rowid")

    def chunks_for_source(self, source_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM chunks WHERE source_id=? ORDER BY idx", (source_id,)
        )

    def evidences_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("evidences", run_id, "rowid")

    def decisions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("decisions", run_id, "rowid")

    def claims_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("claims", run_id, "rowid")

    def report_for_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM reports WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)
        )
        return rows[0] if rows else None

    def verifications_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("verifications", run_id, "id")

    def events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("events", run_id, "id")

    def usage_for_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM usage WHERE run_id=?", (run_id,))
        return rows[0] if rows else None

    # ---------- interpretable coverage metrics ----------
    _SUPPORT_RE = re.compile(r"\[support:(supported|contradicted|insufficient|mixed)\]")
    _FONTI_NUM_RE = re.compile(r"^(\d+)\.\s", re.MULTILINE)
    _CITE_RE = re.compile(r"\[(\d+)\]")

    def metrics_for_run(self, run_id: str) -> dict[str, Any]:
        """Metriche di copertura interpretabili per un run, con numeratore e
        denominatore espliciti dove hanno senso.

        - subquestion_coverage: piano vs ultimo gap status SALVATO
          (cycle_registry.gap_status; fallback: evento gap_detected.status)
        - claim_support: da verifications (verdict + prefisso [support:X] in
          reason); i claim senza prefisso contano come not_conclusive
        - citation_correctness: marker [n] nel markdown salvato e quanti hanno
          una destinazione nella bibliografia "## Fonti"
        - reading_yield: fonti acquisite (dedup per content_hash) / leggibili /
          produttrici di evidenze
        - cycle_contribution: lista dei contributi per ciclo (eventi salvati)
        """
        return {
            "run_id": run_id,
            "subquestion_coverage": self._coverage_metrics(run_id),
            "claim_support": self._claim_support_metrics(run_id),
            "citation_correctness": self._citation_metrics(run_id),
            "reading_yield": self._reading_yield_metrics(run_id),
            "cycle_contribution": self._cycle_contribution_list(run_id),
        }

    def _coverage_metrics(self, run_id: str) -> dict[str, Any]:
        sqs = self._query("SELECT id FROM subquestions WHERE run_id=?", (run_id,))
        plan_total = len(sqs)
        gap_status, source = self._last_gap_status(run_id)
        covered = partial = unresolved = 0
        for v in gap_status.values():
            if v == "coperta":
                covered += 1
            elif v == "parziale":
                partial += 1
            else:
                unresolved += 1
        # sotto-domande del piano assenti dall'ultimo status = non risolte
        unresolved = max(unresolved, plan_total - covered - partial)
        return {
            "plan_total": plan_total,
            "covered": covered,
            "partial": partial,
            "unresolved": unresolved,
            "covered_of": plan_total,
            "covered_ratio": (round(covered / plan_total, 3)
                              if plan_total else None),
            "source": source,
        }

    def _last_gap_status(self, run_id: str) -> tuple[dict[str, str], str]:
        """Ultimo stato di copertura salvato: gap_status dell'ultimo evento
        cycle_registry; fallback all'ultimo evento gap_detected (run piu'
        vecchi del registro di ciclo)."""
        status: dict[str, str] | None = None
        source = ""
        for row in self._query(
                "SELECT payload_json FROM events WHERE run_id=? AND "
                "type='cycle_registry' ORDER BY id", (run_id,)):
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                continue
            gs = payload.get("gap_status")
            if isinstance(gs, dict) and gs:
                status, source = gs, "cycle_registry.gap_status"
        if status is None:
            for row in self._query(
                    "SELECT payload_json FROM events WHERE run_id=? AND "
                    "type='gap_detected' ORDER BY id", (run_id,)):
                try:
                    payload = json.loads(row["payload_json"])
                except ValueError:
                    continue
                st = payload.get("status")
                if isinstance(st, dict) and st:
                    status, source = st, "gap_detected.status"
        return (status or {}), source

    def _claim_support_metrics(self, run_id: str) -> dict[str, Any]:
        ver = self._query(
            "SELECT verdict, reason FROM verifications WHERE run_id=? ORDER BY id",
            (run_id,))
        support = {"supported": 0, "contradicted": 0,
                   "insufficient": 0, "mixed": 0}
        verdicts: dict[str, int] = {}
        for row in ver:
            vd = row["verdict"] or "UNKNOWN"
            verdicts[vd] = verdicts.get(vd, 0) + 1
            m = self._SUPPORT_RE.search(row["reason"] or "")
            if m:
                support[m.group(1)] += 1
        stated = sum(support.values())
        return {
            "verifiable": len(ver),
            **support,
            "not_conclusive": max(0, len(ver) - stated),
            "verdicts": verdicts,
        }

    def _citation_metrics(self, run_id: str) -> dict[str, Any]:
        inline_markers = with_target = 0
        rep = self.report_for_run(run_id)
        if rep and rep.get("markdown"):
            body, _sep, fonti = rep["markdown"].partition("## Fonti")
            targets = {int(m.group(1))
                       for m in self._FONTI_NUM_RE.finditer(fonti)}
            markers = self._CITE_RE.findall(body)
            inline_markers = len(markers)
            with_target = sum(1 for n in markers if int(n) in targets)
        return {
            "inline_markers": inline_markers,
            "with_target": with_target,
            "without_target": inline_markers - with_target,
        }

    def _reading_yield_metrics(self, run_id: str) -> dict[str, Any]:
        src = self._query(
            "SELECT id, fetch_status, accepted, content_hash FROM sources "
            "WHERE run_id=?", (run_id,))
        ok_rows = [r for r in src
                   if r["fetch_status"] in ("SUCCESS", "PARTIAL_CONTENT")]
        seen_hashes: set[str] = set()
        acquired = 0
        for r in ok_rows:  # dedup per contenuto: una copia non e' una fonte nuova
            h = r["content_hash"] or ""
            if h:
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
            acquired += 1
        readable = sum(1 for r in ok_rows if r["accepted"] == 1)
        produced = self._query(
            "SELECT COUNT(DISTINCT source_id) AS n FROM evidences WHERE run_id=?",
            (run_id,))[0]["n"]
        return {
            "acquired": acquired,
            "of_fetch_attempts": len(src),
            "readable": readable,
            "of_acquired": acquired,
            "produced_evidence": produced,
            "of_readable": readable,
            "produced_ratio": (round(produced / readable, 3)
                               if readable else None),
        }

    def _cycle_contribution_list(self, run_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self._query(
                "SELECT cycle, payload_json FROM events WHERE run_id=? AND "
                "type='cycle_contribution' ORDER BY id", (run_id,)):
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                continue
            entry: dict[str, Any] = {"cycle": row["cycle"]}
            entry.update(payload)
            out.append(entry)
        return out
