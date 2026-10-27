"""CITAZIONI: renumber_citations con risoluzione per POSIZIONE, liste [a, b],
fallback per numero (compat old run) e coerenza bibliografia.

La firma e' congelata (verifier.py la chiama): questi test verificano che il
comportamento nuovo (span trovato -> numero del claim giusto) coesista con il
vecchio mapping per numero (span non trovato -> comportamento storico).
"""
from __future__ import annotations

import re

from app.agent.citations import (format_numbers, marker_numbers,
                                 renumber_citations, sanitize_brackets)
from app.models import ReportClaim


def _claim(text: str, no: int, verdict: str | None = "VERIFIED",
           **kw) -> ReportClaim:
    c = ReportClaim(text=text, citation_no=no, evidence_ids=[f"ev{no}"])
    if verdict:
        c.verdict = verdict
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# -- risoluzione per posizione ------------------------------------------------

def test_renumber_by_position_not_by_number():
    # i marker [13] e [5] non corrispondono ad alcun citation_no: col vecchio
    # mapping per numero sarebbero stati rimossi come dangling. Per posizione
    # ogni frase porta il numero finale del SUO claim.
    md = "Il PVT scala del 20% [13]. Le vendite crescono [5]."
    c_pvt = _claim("Il PVT scala del 20%.", 3)
    c_vend = _claim("Le vendite crescono.", 1)
    out, claims = renumber_citations(md, [c_vend, c_pvt])
    assert out == "Il PVT scala del 20% [1]. Le vendite crescono [2]."
    by_text = {c.text: c for c in claims}
    assert by_text["Il PVT scala del 20%."].final_citation_no == 1
    assert by_text["Le vendite crescono."].final_citation_no == 2


def test_position_order_is_span_order_not_claim_number_order():
    # claim 2 prima nel testo, claim 1 dopo: la numerazione segue il testo
    md = "Beta viene prima [9]. Alpha viene dopo [8]."
    c1 = _claim("Alpha viene dopo.", 1)
    c2 = _claim("Beta viene prima.", 2)
    out, claims = renumber_citations(md, [c1, c2])
    assert out == "Beta viene prima [1]. Alpha viene dopo [2]."
    by_text = {c.text: c for c in claims}
    assert by_text["Beta viene prima."].final_citation_no == 1
    assert by_text["Alpha viene dopo."].final_citation_no == 2


def test_mid_sentence_marker_is_owned_by_the_span():
    md = "Il PVT [13] è alto."
    c = _claim("Il PVT è alto.", 3)
    out, claims = renumber_citations(md, [c])
    assert out == "Il PVT è alto [1]."
    assert claims[0].final_citation_no == 1


# -- liste [a, b] --------------------------------------------------------------

def test_list_marker_renumbered():
    md = "Primo dato [2, 3]. Secondo dato [1]."
    claims = [_claim("claim 1", 1), _claim("claim 2", 2), _claim("claim 3", 3)]
    out, claims = renumber_citations(md, claims)
    assert out == "Primo dato [1, 2]. Secondo dato [3]."
    by_old = {c.citation_no: c for c in claims}
    assert [by_old[n].final_citation_no for n in (1, 2, 3)] == [3, 1, 2]


def test_list_number_without_claim_dropped():
    md = "Dato [2, 9]."
    out, claims = renumber_citations(md, [_claim("claim 2", 2)])
    assert out == "Dato [1]."
    assert claims[0].final_citation_no == 1


def test_duplicate_numbers_in_list_deduplicated():
    md = "Doppio [1, 1]."
    out, _ = renumber_citations(md, [_claim("claim 1", 1)])
    assert out == "Doppio [1]."


# -- fallback per numero (compat old run) --------------------------------------

def test_number_fallback_when_span_not_found():
    # i testi dei claim non compaiono nel markdown (vecchi run): vale il
    # comportamento storico basato sul mapping per numero
    md = "X [3]. Y [7]."
    out, claims = renumber_citations(md, [_claim("claim 3", 3),
                                          _claim("claim 7", 7)])
    assert out == "X [1]. Y [2]."
    by_old = {c.citation_no: c for c in claims}
    assert by_old[3].final_citation_no == 1
    assert by_old[7].final_citation_no == 2


# -- claim rimosso: nessun buco -----------------------------------------------

def test_removed_claim_leaves_no_hole_in_numbering():
    # il verifier ha cancellato la frase del claim 2 (REMOVED): i numeri nel
    # testo restano contigui e coerenti con i final_citation_no
    md = "PVT alta [1]. Dato finale [3]."
    claims = [_claim("PVT alta.", 1), _claim("Frase rimossa.", 2, "REMOVED"),
              _claim("Dato finale.", 3)]
    out, claims = renumber_citations(md, claims)
    assert out == "PVT alta [1]. Dato finale [2]."
    by_old = {c.citation_no: c for c in claims}
    assert by_old[3].final_citation_no == 2
    assert by_old[2].final_citation_no is None


def test_dropped_claim_marker_is_removed_without_breaking_text():
    # la frase del claim rimosso e' ancora nel testo (verifier non l'ha
    # trovata): il suo marker sparise ma la frase resta leggibile
    md = "A [1]. B [2]. C [3]."
    claims = [_claim("A.", 1), _claim("B.", 2, "REMOVED"), _claim("C.", 3)]
    out, claims = renumber_citations(md, claims)
    assert out == "A [1]. B. C [2]."
    by_old = {c.citation_no: c for c in claims}
    assert by_old[2].final_citation_no is None
    assert by_old[3].final_citation_no == 2


# -- sanitizzazione difensiva (draft vecchi non passati dal nuovo writer) ------

def test_malformed_tokens_removed_by_renumber():
    md = "A [ev_9ab] [9b->5ab1469bea24, vedi claim 16] [2]."
    out, claims = renumber_citations(md, [_claim("A.", 2)])
    assert out == "A [1]."
    assert "ev_9ab" not in out and "5ab1469bea24" not in out


def test_sanitize_brackets_units():
    assert sanitize_brackets("A [ev_1] B [3]") == "A  B [3]"
    assert sanitize_brackets("[9b->abc, vedi claim 16]") == ""
    # link e immagini markdown restano intatti
    keep = "[link](http://x) e ![img](y) [4]"
    assert sanitize_brackets(keep) == keep
    assert marker_numbers("[10, 20]") == [10, 20]
    assert format_numbers([2, 2, 3]) == "[2, 3]"


# -- coerenza bibliografia per costruzione -------------------------------------

def test_every_inline_number_has_a_final_number():
    # proprietà P0-2: ogni [n] presente nel testo finale e' il final number di
    # un claim -> la bibliografia (costruita dai final_citation_no) ha una voce
    # per ogni numero presente nel testo.
    md = "Alpha [7]. Beta [42]. Gamma senza numero."
    claims = [_claim("Alpha.", 1), _claim("Beta.", 2),
              _claim("Gamma senza numero.", 3)]
    out, claims = renumber_citations(md, claims)
    inline = {int(m) for m in re.findall(r"\[(\d+)\]", out)}
    finals = {c.final_citation_no for c in claims if c.final_citation_no}
    assert inline == {1, 2}
    assert inline <= finals
    assert min(finals) == 1


def test_corrected_claim_uses_corrected_text_span():
    # claim CORRECTED: lo span viene cercato prima col testo corretto
    md = "Bell ottenne il brevetto nel 1876. [1]"
    c = _claim("Bell brevettò il telefono nel 1877.", 1,
               corrected_text="Bell ottenne il brevetto nel 1876.")
    out, claims = renumber_citations(md, [c])
    assert out == "Bell ottenne il brevetto nel 1876. [1]"
    assert claims[0].final_citation_no == 1
