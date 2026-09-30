"""SQLite persistence: runs, queries, sources, evidences, decisions, reports, events."""
from __future__ import annotations

import json
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
  source_type TEXT, quality_score INTEGER, relevance REAL, accepted INTEGER DEFAULT 0
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
  citation_no INTEGER
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
            self._conn.commit()

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
                   redirects: list[str], error: str | None, byte_len: int) -> None:
        self._exec(
            "INSERT INTO sources(id, run_id, url, url_final, domain, title, http_status,"
            " content_type, fetch_status, published_at, author, redirects_json, error, byte_len)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, run_id, url, url_final, domain, title, http_status, content_type,
             fetch_status, published_at, author, json.dumps(redirects), error, byte_len),
        )

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
                  evidence_ids: list[str]) -> None:
        self._exec(
            "INSERT INTO claims(id, run_id, evidence_ids_json, text, citation_no)"
            " VALUES(?,?,?,?,?)",
            (claim_id, run_id, json.dumps(evidence_ids), text, citation_no),
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
