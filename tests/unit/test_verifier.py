"""Tests for Verifier: deterministic + LLM verdicts, markdown surgery."""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.verifier import Verifier
from app.models import DraftReport, Evidence, FetchedDocument, ReportClaim
from tests.fakes import FakeLLMClient


def _tmp_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "verifier.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} C {claim} EV {evidence_json}\n",
        encoding="utf-8")
    return d


def _ev(eid: str, quote: str) -> Evidence:
    return Evidence(evidence_id=eid, source_id="src1", chunk_id="chk1",
                    quote=quote, claim="claim base", run_id="r1")


def _draft(*claims: ReportClaim) -> DraftReport:
    return DraftReport(title="T", markdown=" ".join(c.text for c in claims),
                       claims=list(claims))


async def test_removed_when_no_evidence(tmp_path):
    claim = ReportClaim(text="Frase orfana.", citation_no=1, evidence_ids=[])
    v = Verifier(FakeLLMClient([]), prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", "quote")}, {})

    assert out.claims[0].verdict == "REMOVED"
    assert "Frase orfana." not in out.markdown
    assert out.stats.number_removed == 1


async def test_numeric_downgrade(tmp_path):
    quote = "Bell brevettò il telefono nel 1876."
    claim = ReportClaim(text="Bell brevettò il telefono nel 1901.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([json.dumps({"verdict": "VERIFIED", "reason": "ok"})])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", quote)}, {})

    assert out.claims[0].verdict == "WEAKENED"
    assert "1901" in out.claims[0].verdict_reason
    assert out.stats.number_failed == 1  # weakened counted as failed check


async def test_corrected_replaces_markdown(tmp_path):
    quote = "Bell brevettò il telefono nel 1876."
    claim = ReportClaim(text="Bell brevettò il telefono nel 1877.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([json.dumps({
        "verdict": "CORRECTED", "reason": "wrong year",
        "corrected_text": "Bell brevettò il telefono nel 1876."})])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", quote)}, {})

    assert out.claims[0].verdict == "CORRECTED"
    assert "1876." in out.markdown and "1877" not in out.markdown
    assert out.stats.number_corrected == 1


async def test_llm_error_marks_failed(tmp_path):
    class Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("down")

    claim = ReportClaim(text="Frase.", citation_no=1, evidence_ids=["ev1"])
    v = Verifier(Boom(), prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", "quote")}, {})
    assert out.claims[0].verdict == "FAILED"


async def test_verified_happy_path(tmp_path):
    claim = ReportClaim(text="Bell brevettò il telefono nel 1876.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([json.dumps({"verdict": "VERIFIED", "reason": "match"})])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim),
                         {"ev1": _ev("ev1", "Bell brevettò il telefono nel 1876.")},
                         {"src1": FetchedDocument(source_id="src1", url="u")})
    assert out.stats.number_verified == 1
    assert out.stats.number_of_claims == 1
