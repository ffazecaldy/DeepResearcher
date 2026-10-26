"""Regression: tassonomia cause + errori tecnici non conclusivi + seconda
verifica della formulazione corretta (verifier).

- [cause:Y] arriva DOPO il prefisso [support:X] nel verdict_reason; il
  prefisso resta compatibile con la regex dell'orchestrator
- errore tecnico / output non valido -> FAILED "verifica non conclusa",
  MAI un esito di merito ("affermazione falsa")
- CORRECTED: la SOLA formulazione corretta viene riverificata; se non e'
  supportata anche lei -> degrada a WEAKENED (anti-vago)
- warning quando la quote non e' nel documento sorgente (diagnosi post-run)
"""
from __future__ import annotations

import json
import logging
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


async def test_cause_prefix_after_support_prefix(tmp_path):
    """[cause:Y] dopo [support:X]; il prefisso resta primo e regex-compatibile."""
    from app.agent.orchestrator import _SUPPORT_RE

    claim = ReportClaim(text="Bell lavorò al telefono.", citation_no=1,
                        evidence_ids=["ev1"])
    fake = FakeLLMClient([json.dumps({"verdict": "WEAKENED", "reason": "vaga"})])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", "quote")}, {})

    r = out.claims[0].verdict_reason
    assert r.startswith("[support:insufficient] ")
    assert "[cause:" in r
    m = _SUPPORT_RE.search(r)  # stessa regex usata dall'orchestrator
    assert m is not None and m.group(1) == "insufficient"


async def test_cause_from_llm_passthrough(tmp_path):
    """Causa valida dalla tassonomia restituita dall'LLM -> usata verbatim."""
    claim = ReportClaim(text="Il 90% degli utenti è soddisfatto.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([json.dumps({
        "verdict": "WEAKENED", "support": "supported",
        "cause": "overstated", "reason": "fonte dice 60%"})])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim),
                         {"ev1": _ev("ev1", "60% soddisfatto")}, {})

    r = out.claims[0].verdict_reason
    assert r.startswith("[support:supported] [cause:overstated] ")


async def test_removed_no_evidence_has_cause(tmp_path):
    """Claim orfano -> prefisso + causa no_relevant_evidence."""
    claim = ReportClaim(text="Frase orfana.", citation_no=1, evidence_ids=[])
    v = Verifier(FakeLLMClient([]), prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", "quote")}, {})

    assert out.claims[0].verdict == "REMOVED"
    assert out.claims[0].verdict_reason.startswith(
        "[support:insufficient] [cause:no_relevant_evidence] ")


async def test_technical_error_is_non_conclusive(tmp_path):
    """Timeout/errore API -> FAILED non conclusivo, MAI 'affermazione falsa'."""

    class Boom:
        async def complete(self, *a, **k):
            raise TimeoutError("rate limit")

    claim = ReportClaim(text="Frase.", citation_no=1, evidence_ids=["ev1"])
    v = Verifier(Boom(), prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", "quote")}, {})

    c = out.claims[0]
    assert c.verdict == "FAILED"
    assert c.verdict_reason.startswith("[support:insufficient] "
                                       "[cause:technical_error] ")
    assert "verifica non conclusa" in c.verdict_reason
    assert "falsa" not in c.verdict_reason


async def test_invalid_output_is_non_conclusive(tmp_path):
    """JSON o verdict invalido -> FAILED invalid_output, non conclusivo."""

    class Garbage:
        async def complete(self, *a, **k):
            return "risposta senza json {{{"

    claim = ReportClaim(text="Frase.", citation_no=1, evidence_ids=["ev1"])
    v = Verifier(Garbage(), prompts_dir=_tmp_prompts(tmp_path))

    out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", "quote")}, {})

    c = out.claims[0]
    assert c.verdict == "FAILED"
    assert "[cause:invalid_output]" in c.verdict_reason
    assert "verifica non conclusa" in c.verdict_reason


async def test_recheck_degrades_unsupported_corrected(tmp_path):
    """Anche la formulazione corretta non supportata -> WEAKENED (anti-vago)."""
    quote = "Bell brevettò il telefono nel 1876."
    claim = ReportClaim(text="Bell brevettò il telefono nel 1877.",
                        citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([
        json.dumps({"verdict": "CORRECTED", "reason": "anno errato",
                    "corrected_text": "Bell brevettò il telefono."}),
        json.dumps({"verdict": "WEAKENED", "support": "insufficient",
                    "reason": "resta generica"}),
    ])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))
    md = "Bell brevettò il telefono nel 1877."

    out = await v.verify("q", DraftReport(title="T", markdown=md, claims=[claim]),
                         {"ev1": _ev("ev1", quote)}, {})

    c = out.claims[0]
    assert c.verdict == "WEAKENED"
    assert "formulazione corretta non e' supportata" in c.verdict_reason
    assert out.stats.number_corrected == 0
    assert out.stats.number_failed == 1
    # il markdown NON e' stato riscritto, il claim non ha corrected_text
    assert "1877" in out.markdown
    assert c.corrected_text is None


async def test_recheck_confirmed_keeps_corrected(tmp_path):
    """Formulazione corretta supportata dalla seconda verifica -> CORRECTED."""
    quote = "Bell brevettò il telefono nel 1876."
    claim = ReportClaim(text="Bell brevettò il telefono nel 1877.",
                        citation_no=1, evidence_ids=["ev1"])
    corrected = "Bell brevettò il telefono nel 1876."
    fake = FakeLLMClient([
        json.dumps({"verdict": "CORRECTED", "reason": "anno errato",
                    "corrected_text": corrected}),
        json.dumps({"verdict": "VERIFIED", "support": "supported",
                    "reason": "ok"}),
    ])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))
    md = "Bell brevettò il telefono nel 1877."

    out = await v.verify("q", DraftReport(title="T", markdown=md, claims=[claim]),
                         {"ev1": _ev("ev1", quote)}, {})

    c = out.claims[0]
    assert c.verdict == "CORRECTED"
    assert c.corrected_text == corrected
    assert "1876." in out.markdown and "1877" not in out.markdown
    assert out.stats.number_corrected == 1


async def test_recheck_technical_failure_keeps_corrected(tmp_path):
    """Seconda verifica non eseguibile -> resta CORRECTED (prima verifica valida)."""
    calls = {"n": 0}

    class Flaky:
        async def complete(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps({"verdict": "CORRECTED", "reason": "anno",
                                   "corrected_text": "Bell brevettò il telefono."})
            raise TimeoutError("rate limit")

    claim = ReportClaim(text="Bell brevettò il telefono nel 1877.",
                        citation_no=1, evidence_ids=["ev1"])
    v = Verifier(Flaky(), prompts_dir=_tmp_prompts(tmp_path))
    md = "Bell brevettò il telefono nel 1877."

    out = await v.verify("q", DraftReport(title="T", markdown=md, claims=[claim]),
                         {"ev1": _ev("ev1", "Bell brevettò il telefono nel 1876.")},
                         {})

    assert calls["n"] == 2  # verifica + tentativo di seconda verifica
    assert out.claims[0].verdict == "CORRECTED"


async def test_quote_missing_from_source_warns(tmp_path, caplog):
    """Quote assente dal documento sorgente -> warning con evidence_id."""
    quote = "Frase mai scritta da nessuna parte."
    claim = ReportClaim(text="Frase.", citation_no=1, evidence_ids=["ev1"])
    fake = FakeLLMClient([json.dumps({"verdict": "VERIFIED", "reason": "ok"})])
    v = Verifier(fake, prompts_dir=_tmp_prompts(tmp_path))
    doc = FetchedDocument(source_id="src1", url="u",
                          text="Testo completamente diverso.")

    with caplog.at_level(logging.WARNING, logger="app.agent.verifier"):
        out = await v.verify("q", _draft(claim), {"ev1": _ev("ev1", quote)},
                             {"src1": doc})

    assert out.claims[0].verdict == "VERIFIED"  # il warning non cambia il verdict
    assert any("ev1" in rec.message and "non presente" in rec.message
               for rec in caplog.records)
