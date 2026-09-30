"""Pydantic domain models shared across the pipeline."""
from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    """Short prefixed id for local, single-user runs."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class FetchStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL_CONTENT = "PARTIAL_CONTENT"
    FETCH_FAILED = "FETCH_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class DecisionKind(StrEnum):
    BINARY = "BINARY"
    CHOICE = "CHOICE"
    SCORE = "SCORE"


Verdict = Literal["VERIFIED", "WEAKENED", "CORRECTED", "REMOVED", "FAILED"]


class DecisionSpec(BaseModel):
    name: str
    kind: DecisionKind
    question: str
    options: dict[str, str] | None = None  # for CHOICE
    scale: list[str] | None = None  # for SCORE (ordinal levels, low -> high)


class DecisionResult(BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    name: str
    kind: DecisionKind
    engine: str  # "laya" | "llm"
    value: bool | str | int | None = None
    confidence: float | None = None
    abstained: bool = False
    latency_ms: float = 0.0
    subject_kind: str = ""  # "source" | "evidence" | ""
    subject_id: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class SubQuestion(BaseModel):
    subquestion_id: str
    text: str
    idx: int


class GeneratedQuery(BaseModel):
    query_id: str
    subquestion_id: str
    text: str
    cycle: int = 1


class Plan(BaseModel):
    title: str
    language: str
    subquestions: list[SubQuestion] = Field(min_length=1)
    queries: list[GeneratedQuery] = Field(min_length=1)


class SearchResultItem(BaseModel):
    query_id: str
    url: str
    title: str = ""
    snippet: str = ""
    position: int = 0


class FetchedDocument(BaseModel):
    source_id: str = Field(default_factory=lambda: new_id("src"))
    url: str
    final_url: str = ""
    domain: str = ""
    title: str = ""
    text: str = ""
    http_status: int | None = None
    content_type: str = ""
    published_at: str | None = None
    author: str | None = None
    redirects: list[str] = Field(default_factory=list)
    fetch_status: FetchStatus = FetchStatus.FETCH_FAILED
    error: str | None = None
    byte_len: int = 0


class Chunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: new_id("chk"))
    source_id: str
    idx: int
    text: str
    char_start: int
    char_end: int


class Evidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: new_id("ev"))
    run_id: str = ""
    source_id: str
    chunk_id: str
    quote: str
    span_start: int | None = None
    span_end: int | None = None
    claim: str
    subquestion_id: str | None = None
    source_type: str | None = None
    quality_score: int | None = None
    confidence: float | None = None


class NewQuery(BaseModel):
    subquestion_id: str
    text: str


class GapReport(BaseModel):
    complete: bool
    covered_subquestions: list[str] = Field(default_factory=list)
    missing_subquestions: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    new_queries: list[NewQuery] = Field(default_factory=list)


class ReportClaim(BaseModel):
    claim_id: str = Field(default_factory=lambda: new_id("clm"))
    text: str
    citation_no: int
    evidence_ids: list[str] = Field(default_factory=list)
    verdict: Verdict | None = None
    verdict_reason: str | None = None
    corrected_text: str | None = None


class DraftReport(BaseModel):
    title: str
    markdown: str  # body WITHOUT the Fonti section (added deterministically)
    claims: list[ReportClaim] = Field(default_factory=list)


class VerificationStats(BaseModel):
    number_of_claims: int = 0
    number_verified: int = 0
    number_corrected: int = 0
    number_removed: int = 0
    number_failed: int = 0


class VerifiedReport(BaseModel):
    markdown: str
    claims: list[ReportClaim] = Field(default_factory=list)
    stats: VerificationStats = Field(default_factory=VerificationStats)


class Event(BaseModel):
    type: str
    run_id: str
    ts: float = Field(default_factory=time.time)
    cycle: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class RunOutcome(BaseModel):
    run_id: str
    status: str
    question: str
    language: str
    markdown_path: str | None = None
    json_path: str | None = None
    limit_reached: bool = False
    limit_note: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
