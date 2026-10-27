"""Regression P0-4 (verifier): guardie deterministiche a valle.

- POPOLAZIONE: evidenze con popolazione esplicita (ADHD/bambini) + claim
  generico -> WEAKENED, prefisso [support:X] invariato, [cause:overstated];
  claim che mantiene la popolazione -> resta VERIFIED
- ATTRIBUZIONE: fatto scientifico forte ("dimostra") su sola fonte
  secondaria (news) -> verdict massimo WEAKENED "fonte secondaria:
  richiede attribuzione"; su fonte primaria (paper) -> resta VERIFIED
- META-CLAIMS: "le evidenze sono contrastanti" senza riferimenti concreti
  nel reason -> WEAKENED "meta-affermazione non supportata"; reason con
  evidenza concreta -> resta VERIFIED
"""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.verifier import Verifier
from app.models import DraftReport, Evidence, ReportClaim
from tests.fakes import FakeLLMClient

QUOTE_ADHD = ("Lo studio non ha migliorato i sintomi core ADHD in bambini "
              "e adolescenti.")


def _tmp_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "verifier.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} C {claim} EV {evidence_json}\n",
        encoding="utf-8")
    return d


def _ev(eid: str, quote: str, source_type: str | None = None) -> Evidence:
    return Evidence(evidence_id=eid, source_id="src1", chunk_id="chk1",
                    quote=quote, claim="claim base", run_id="r1",
                    source_type=source_type)


def _draft(claim: ReportClaim) -> DraftReport:
    return DraftReport(title="T", markdown=claim.text, claims=[claim])


def _verified(claim: ReportClaim, reason: str = "ok") -> str:
    return json.dumps({"verdict": "VERIFIED", "support": "supported",
                       "reason": reason})


async def test_population_guard_weakens_generic_claim(tmp_path):
    """Quote ADHD/bambini + claim generico: VERIFIED -> WEAKENED overstated."""
    claim = ReportClaim(text="L'integrazione migliora l'attenzione.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([_verified(claim)])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim),
                         {"ev1": _ev("ev1", QUOTE_ADHD)}, {})

    c = out.claims[0]
    assert c.verdict == "WEAKENED"
    assert c.verdict_reason.startswith("[support:supported] ")
    assert "[cause:overstated]" in c.verdict_reason
    assert "popolazione" in c.verdict_reason


async def test_population_guard_spares_narrowed_claim(tmp_path):
    """Claim che mantiene la popolazione (restringe): resta VERIFIED."""
    claim = ReportClaim(text="Nei bambini con ADHD l'attenzione non migliora.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([_verified(claim)])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim),
                         {"ev1": _ev("ev1", QUOTE_ADHD)}, {})

    assert out.claims[0].verdict == "VERIFIED"


async def test_secondary_source_caps_strong_fact(tmp_path):
    """Fatto forte ("dimostra") su sola fonte news: max WEAKENED + attribuzione."""
    claim = ReportClaim(text="Questo studio dimostra che l'integrazione "
                             "riduce i sintomi.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([_verified(claim)])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify(
        "q", _draft(claim),
        {"ev1": _ev("ev1", "L'integrazione e' stata somministrata per "
                            "dodici settimane.", source_type="news")}, {})

    c = out.claims[0]
    assert c.verdict == "WEAKENED"
    assert c.verdict_reason.startswith("[support:supported] ")
    assert "fonte secondaria: richiede attribuzione" in c.verdict_reason


async def test_primary_source_keeps_strong_fact(tmp_path):
    """Fatto forte su fonte primaria (paper): nessuna cap, resta VERIFIED."""
    claim = ReportClaim(text="Questo studio dimostra che l'integrazione "
                             "riduce i sintomi.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([_verified(claim)])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify(
        "q", _draft(claim),
        {"ev1": _ev("ev1", "L'integrazione e' stata somministrata per "
                            "dodici settimane.", source_type="paper")}, {})

    assert out.claims[0].verdict == "VERIFIED"


# quote SENZA popolazione: i test meta-claim devono esercitare SOLO la
# guardia meta (la guardia popolazione e' coperta dai test dedicati)
QUOTE_NEUTRA = "L'integrazione di omega-3 e' stata assunta per dodici settimane."


async def test_baseless_meta_claim_weakened(tmp_path):
    """'evidenze contrastanti' senza riferimenti concreti -> WEAKENED."""
    claim = ReportClaim(text="Le evidenze sono contrastanti "
                             "sull'effetto dell'integrazione.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([_verified(claim, reason="le evidenze sono "
                                                "contrastanti")])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim),
                         {"ev1": _ev("ev1", QUOTE_NEUTRA)}, {})

    c = out.claims[0]
    assert c.verdict == "WEAKENED"
    assert "meta-affermazione non supportata" in c.verdict_reason


async def test_meta_claim_with_concrete_reason_kept(tmp_path):
    """Reason che cita l'evidenza concreta (id): il meta-claim resta VERIFIED."""
    claim = ReportClaim(text="Le evidenze sono contrastanti "
                             "sull'effetto dell'integrazione.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([_verified(claim,
                                    reason="ev1 nega il miglioramento "
                                           "dei sintomi")])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim),
                         {"ev1": _ev("ev1", QUOTE_NEUTRA)}, {})

    assert out.claims[0].verdict == "VERIFIED"
