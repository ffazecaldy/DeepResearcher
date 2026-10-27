"""Validazione published_at — B14 (bug reale PubMed 33201262).

trafilatura aveva preso l'anno "1995" dalla BIBLIOGRAFIA interna della pagina
(voce "Benjamini Y, Hochberg Y. ... J R Stat Soc B 1995") mentre la copia PMC
corretta diceva 2020-11-17. Ora:

- le date strutturate (JSON-LD datePublished, meta citation_*) vincono sempre;
- una data trovata solo in righe da bibliografia viene scartata;
- plausibilita' 1900..oggi+1;
- nessuna data valida -> published_at resta None (mai inventata; la data di
  RECUPERO e' un'altra cosa: fetched_at/created_at del documento).
"""
from __future__ import annotations

from app.extraction.extract import (extract_html, validate_date,
                                    _bibliography_line, _only_in_bibliography,
                                    _resolve_published_at, _trusted_date)

ABSTRACT = ("Risultati dello studio clinico condotto su 480 pazienti "
            "ricoverati con polmonite interstiziale confermata. "
            "La coorte evidenzia una riduzione significativa della carica "
            "virale nei pazienti trattati con antivirali rispetto al gruppo "
            "di controllo osservazionale durante il ricovero. ")

REF_1995 = ("Benjamini Y, Hochberg Y. Controlling the false discovery rate: "
            "a practical and powerful approach to multiple testing. "
            "J R Stat Soc B 1995;57(1):289-300.")

REF_1998 = ("Simes RJ. An optimized Bonferroni multiple testing procedure. "
            "Biometrika 1998;85(2):379-386 et al.")


def _page(*, citation_date: str | None = None, references: str = "",
          body: str = ABSTRACT, jsonld: str | None = None) -> str:
    head_meta = (f'<meta name="citation_date" content="{citation_date}">'
                 if citation_date else "")
    ld = (f'<script type="application/ld+json">{jsonld}</script>'
          if jsonld else "")
    return (f"<html><head><title>Studio clinico</title>{head_meta}{ld}"
            f"</head><body><p>{body}</p>"
            f"<div class=\"references\">{references}</div></body></html>")


# ---------- validate_date: plausibilita' e normalizzazione ----------
def test_validate_date_normalization():
    assert validate_date("2020-11-17") == "2020-11-17"
    assert validate_date("2020/11/17") == "2020-11-17"
    assert validate_date("2020-11-17T10:00:00Z") == "2020-11-17"
    assert validate_date("March 5, 2021") == "2021-03-05"
    assert validate_date("2020-11") == "2020-11"
    assert validate_date("1995") == "1995"   # anno plausibile: la bibliografia decide dopo
    assert validate_date("1900") == "1900"   # boundary: >= 1900


def test_validate_date_implausible_or_garbage_is_none():
    assert validate_date("1876") is None       # < 1900
    assert validate_date("1899") is None
    assert validate_date("1876-02-17") is None
    assert validate_date("2050") is None       # > oggi+1
    assert validate_date("2050-01-01") is None
    assert validate_date("1995-13-40") is None  # mese/giorno impossibili
    assert validate_date("n/d") is None
    assert validate_date("") is None
    assert validate_date(None) is None


# ---------- euristica bibliografia ----------
def test_bibliography_line_heuristic():
    assert _bibliography_line(REF_1995)      # autori + journal abbreviato + vol:pagine
    assert _bibliography_line(REF_1998)      # "et al"
    assert not _bibliography_line(
        "La legge fu approvata nel 1995 dal parlamento europeo in seduta "
        "plenaria dopo un lungo dibattito.")  # prosa: nessun autore/journal
    assert not _bibliography_line("Il metodo fu descritto in anni recenti.")


def test_only_in_bibliography():
    bib_page = _page(references=REF_1995)
    assert _only_in_bibliography(bib_page, "1995") is True
    mixed = _page(body=ABSTRACT + " Antecedenti risalgono al 1995 in un "
                  "contesto diverso. ", references=REF_1995)
    assert _only_in_bibliography(mixed, "1995") is False  # anche nel corpo


# ---------- il caso reale ----------
def test_pubmed_33201262_structured_date_wins_over_bibliography():
    """La pagina PubMed ha citation_date 2020/11/17 e una bibliografia con
    l'anno 1995: published_at DEVE essere 2020-11-17 (caso reale corretto
    dalla copia PMC), mai 1995."""
    meta = extract_html(_page(citation_date="2020/11/17",
                              references=REF_1995),
                        "https://pubmed.ncbi.nlm.nih.gov/33201262/")[1]
    assert meta["published_at"] == "2020-11-17"
    assert _trusted_date(_page(citation_date="2020/11/17",
                               references=REF_1995)) == "2020-11-17"


def test_bibliography_year_discarded_yields_none():
    """NESSUNA data strutturata e l'unica data presente e' in bibliografia:
    published_at None (prima usciva '1995' — la data della CITAZIONE interna,
    non della pagina). Deterministico: se trafilatura non trova nulla il
    risultato e' comunque None."""
    meta = extract_html(_page(references=REF_1995),
                        "https://example.org/studio")[1]
    assert meta["published_at"] is None


def test_jsonld_date_wins():
    """JSON-LD datePublished (anche con tempo/timezone) batte qualsiasi
    candidata text-level."""
    page = _page(jsonld='{"@type":"ScholarlyArticle",'
                        '"datePublished":"2020-03-04T10:00:00Z"}',
                 references=REF_1998)
    meta = extract_html(page, "https://journal.example/article")[1]
    assert meta["published_at"] == "2020-03-04"


def test_implausible_candidate_yields_none():
    """Anno pre-1900 nel testo (es. data storica citata): scartato, non
    inventata una data a caso."""
    page = _page(body=ABSTRACT + " Il metodo si ispira alla celebre spedizione "
                 "del 1876 verso il polo nord. ", references="")
    meta = extract_html(page, "https://example.org/storico")[1]
    assert meta["published_at"] is None


def test_no_date_at_all_stays_none():
    meta = extract_html(_page(), "https://example.org/nodate")[1]
    assert meta["published_at"] is None


def test_resolve_published_at_precedence():
    html = _page(references=REF_1995)
    # 1. sorgente strutturata vince sempre (anche su candidata text-level)
    assert _resolve_published_at(html, "1995", "2020-11-17") == "2020-11-17"
    # 2. candidata text-level valida ma SOLO in bibliografia -> None
    assert _resolve_published_at(html, "1995", None) is None
    # 3. candidata implausibile -> None
    assert _resolve_published_at(html, "1876", None) is None
    # 4. nessuna candidata -> None (mai inventata)
    assert _resolve_published_at(html, None, None) is None
