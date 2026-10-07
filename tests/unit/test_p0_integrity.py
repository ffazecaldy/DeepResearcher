"""P0 regression tests: report integrity, unified citations, coverage.

These test BEHAVIOR (final markdown / DB / API content), not just verdicts.
"""
from __future__ import annotations

import json

import pytest

from app.models import (Chunk, DraftReport, Evidence, FetchedDocument,
                        FetchStatus, ReportClaim)
from app.agent.citations import renumber_citations
from app.agent.coverage import uncovered_sentences
from app.agent.verifier import _num_tokens


# ---------- P0-2: unified citation numbering ----------

def _claim(no: int, verdict: str | None = None, **kw) -> ReportClaim:
    c = ReportClaim(text=f"claim {no}", citation_no=no, evidence_ids=[f"ev{no}"])
    if verdict:
        c.verdict = verdict
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_two_claims_same_source_keep_distinct_numbers():
    md = "Uno [1]. Due [2]."
    c1, c2 = _claim(1), _claim(2)
    out, claims = renumber_citations(md, [c1, c2])
    assert out == "Uno [1]. Due [2]."
    assert [c.final_citation_no for c in claims] == [1, 2]


def test_claim_with_multiple_sources_uses_first_number():
    md = "A [1]."
    c = ReportClaim(text="A.", citation_no=1, evidence_ids=["ev_a", "ev_b"])
    c.verdict = "VERIFIED"
    out, _ = renumber_citations(md, [c])
    assert out == "A [1]."


def test_non_consecutive_model_numbers_are_normalized():
    md = "X [3]. Y [7]."
    c3, c7 = _claim(3), _claim(7)
    out, claims = renumber_citations(md, [c3, c7])
    assert out == "X [1]. Y [2]."
    by_old = {c.citation_no: c for c in claims}
    assert by_old[3].final_citation_no == 1
    assert by_old[7].final_citation_no == 2


def test_removed_claim_drops_marker_and_closes_gaps():
    # pipeline reale: il verifier rimuove la frase intera, poi si rinumera
    md = "X [1]. Z [3]."
    c1, c2, c3 = _claim(1, "VERIFIED"), _claim(2, "REMOVED"), _claim(3, "VERIFIED")
    out, claims = renumber_citations(md, [c1, c2, c3])
    assert out == "X [1]. Z [2]."
    by_old = {c.citation_no: c for c in claims}
    assert by_old[3].final_citation_no == 2
    assert by_old[2].final_citation_no is None


def test_dangling_marker_without_claim_is_removed():
    md = "Solo [5]."
    out, _ = renumber_citations(md, [])
    assert "[5]" not in out


# ---------- P0-3: numbers + coverage ----------

def test_number_token_not_confused_inside_year():
    # '20' appears ONLY inside '2020' here -> must not be extracted
    assert _num_tokens("nel 2020 crollò del 5%") == {"2020", "5"}
    assert "20" not in _num_tokens("nel 2020 crollò del 5%")
    # a real standalone 20 IS extracted
    assert "20" in _num_tokens("nel 2020 ci sono 20 casi")


def test_uncovered_factual_sentence_is_flagged():
    md = "Il 90% degli utenti abbandona entro 10 secondi."
    out = uncovered_sentences(md, set())
    assert len(out) == 1
    # covered by a claim: no flag
    assert uncovered_sentences(md, {md}) == []


def test_narrative_sentences_are_not_flagged():
    md = "Vediamo ora il quadro complessivo della ricerca."
    assert uncovered_sentences(md, set()) == []


# ---------- P0-1: verifier output reaches the final markdown ----------

@pytest.mark.asyncio
async def test_removed_claim_disappears_from_final_markdown(tmp_path):
    from app.agent.verifier import Verifier
    from tests.fakes import FakeLLMClient
    import json as _j

    class RemovingLLM(FakeLLMClient):
        def __init__(self):
            super().__init__([])

        async def complete(self, system, user, **kw):
            return _j.dumps({"verdict": "REMOVED", "reason": "non supportato"})

    ev = Evidence(evidence_id="ev1", run_id="r", source_id="s1", chunk_id="chk1",
                  subquestion_id="sq1", claim="riepilogo",
                  quote="Il carcere di Robben chiuse nel 1996.")
    v = Verifier(RemovingLLM())
    claim = ReportClaim(text="Il carcere di Robben chiuse nel 1998.",
                        citation_no=1, evidence_ids=["ev1"])
    claim.verdict = None
    draft = DraftReport(title="T", markdown="Il carcere di Robben chiuse nel 1998. [1]",
                        claims=[claim])
    out = await v.verify("q", draft, {"ev1": ev}, {})
    assert "1998" not in out.markdown  # actually removed from the text
    assert out.claims[0].verdict == "REMOVED"


# ---------- bibliography off final numbers ----------

def test_final_markdown_bibliography_matches_final_numbers():
    from app.agent.orchestrator import _final_markdown
    src = FetchedDocument(source_id="s1", url="https://a.example/x",
                          final_url="https://a.example/x", domain="a.example",
                          title="Fonte A", fetch_status=FetchStatus.SUCCESS)
    ev = Evidence(evidence_id="ev2", run_id="r", source_id="s1", chunk_id="chk1",
                  subquestion_id="sq1", claim="c", quote="q")
    c = _claim(3, "VERIFIED")  # model gave 3, final must be 1
    c.evidence_ids = ["ev2"]   # the evidence actually held by this test's store
    c.final_citation_no = 1
    md = _final_markdown("T", "testo [1].", [c], [ev], {"s1": src})
    bib = [ln for ln in md.splitlines() if ln.startswith("1. ")]
    assert bib and bib[0].startswith("1. Fonte A")
