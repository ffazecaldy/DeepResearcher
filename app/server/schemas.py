"""Pydantic request/response models for the HTTP API + language detection."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import Depth


def detect_language(question: str) -> str:
    """Heuristic it/en detection (same as the CLI)."""
    italian_marks = ("à", "è", "é", "ì", "ò", "ù", "perché", "come ", "qual ",
                     "quali", "chi ", "cos'è", "storia ")
    low = question.lower()
    return "it" if any(m in low for m in italian_marks) else "en"


class RunStartRequest(BaseModel):
    question: str = Field(min_length=1)
    language: str | None = None
    depth: Depth = Depth.STANDARD


class RunStartResponse(BaseModel):
    run_id: str
    status: str = "running"


class CancelResponse(BaseModel):
    cancelled: bool = True


class DeleteResponse(BaseModel):
    deleted: bool = True


# The 5 typed decisions of the pipeline (mirrors orchestrator._evaluate_docs
# is_relevant/source_type/quality and its read/verify steps).
DECISION_SPECS: list[dict] = [
    {
        "name": "is_relevant",
        "kind": "BINARY",
        "question": "Does this document contain information useful "
                    "to answer: {question}?",
        "options": None,
        "scale": None,
    },
    {
        "name": "source_type",
        "kind": "CHOICE",
        "question": "What kind of source is this?",
        "options": {"official": "governments, institutions, standards",
                    "paper": "scientific papers, preprints",
                    "news": "journalism",
                    "forum": "community discussions, Q&A",
                    "other": "anything else"},
        "scale": None,
    },
    {
        "name": "quality",
        "kind": "SCORE",
        "question": "Overall reliability of this source for the question?",
        "options": None,
        "scale": ["low", "medium", "high"],
    },
    {
        "name": "extract_evidence",
        "kind": "CHOICE",
        "question": "Which verbatim quote best supports a claim for the "
                    "question? (empty = none)",
        "options": None,
        "scale": None,
    },
    {
        "name": "verify_claim",
        "kind": "CHOICE",
        "question": "Does the evidence support, weaken, correct or remove "
                    "this claim?",
        "options": {"VERIFIED": "the evidence supports the claim",
                    "WEAKENED": "the evidence partially contradicts it",
                    "CORRECTED": "the evidence requires a reformulation",
                    "REMOVED": "the evidence contradicts the claim",
                    "FAILED": "verification could not be performed"},
        "scale": None,
    },
]


def decision_specs_response(settings) -> dict:
    """Payload of GET /api/decision-specs (+ configured default engine)."""
    return {"decision_engine": settings.decision_engine.value,
            "specs": DECISION_SPECS}
