"""CITAZIONI (P0-2b): allineamento POST-WRITE dei marker [n] per POSIZIONE.

Il writer LLM reale emette marker sfasati rispetto ai citation_no del proprio
JSON, liste [a, b] e marker malformati con id interni. Questi test coprono la
sanitizzazione + risoluzione per posizione di Writer.write (offline: fake LLM).
"""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.writer import Writer, _PROMPTS_DIR
from app.models import Evidence, SubQuestion
from tests.fakes import FakeLLMClient


def _tmp_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "writer.txt").write_text(
        "SYS\n---SYSTEM---\nQ {question} L {language} EV {evidence_json} "
        "SQ {subquestions_json}\n", encoding="utf-8")
    return d


def _evs() -> list[Evidence]:
    return [Evidence(evidence_id=f"ev{i}", source_id="src1", chunk_id="chk1",
                     quote="quote", claim="claim", run_id="r1")
            for i in (1, 2, 3)]


def _subs() -> list[SubQuestion]:
    return [SubQuestion(subquestion_id="sq1", text="Chi?", idx=1)]


async def _write(tmp_path: Path, report: dict) -> object:
    fake = FakeLLMClient([json.dumps(report)])
    w = Writer(fake, prompts_dir=_tmp_prompts(tmp_path))
    return await w.write("domanda", "it", _subs(), _evs())


# -- (b) marker sfasati risolti per POSIZIONE --------------------------------

async def test_misaligned_markers_resolved_by_position(tmp_path):
    # la frase PVT porta [13] ma il claim PVT e' il numero 3 del JSON:
    # il marker nel testo DEVE diventare 3. Idem i marker con id interno
    # [ev_9ab] e il malformato [9b->...] vengono rimossi e la frase resta
    # citata col numero del suo claim.
    report = {
        "title": "T",
        "markdown": ("La spesa PVT è cresciuta del 20% [13].\n\n"
                     "Le vendite sono migliorate [ev_9ab].\n\n"
                     "Il margine netto [9b->5ab1469bea24, vedi claim 16] è stabile."),
        "claims": [
            {"text": "Le vendite sono migliorate.", "citation_no": 1,
             "evidence_ids": ["ev1"]},
            {"text": "Il margine netto è stabile.", "citation_no": 2,
             "evidence_ids": ["ev2"]},
            {"text": "La spesa PVT è cresciuta del 20%.", "citation_no": 3,
             "evidence_ids": ["ev3"]},
        ],
    }
    draft = await _write(tmp_path, report)

    assert "La spesa PVT è cresciuta del 20% [3]." in draft.markdown
    assert "Le vendite sono migliorate [1]." in draft.markdown
    assert "Il margine netto è stabile [2]." in draft.markdown
    assert "[13]" not in draft.markdown
    assert "ev_9ab" not in draft.markdown
    assert "5ab1469bea24" not in draft.markdown
    # i citation_no dei claim restano quelli del JSON del writer
    assert [c.citation_no for c in draft.claims] == [1, 2, 3]


async def test_cluster_of_markers_collapses_to_claim_number(tmp_path):
    report = {
        "title": "T",
        "markdown": "Doppio marker [13] [25].",
        "claims": [{"text": "Doppio marker.", "citation_no": 1,
                    "evidence_ids": ["ev1"]}],
    }
    draft = await _write(tmp_path, report)
    assert draft.markdown.strip() == "Doppio marker [1]."


# -- (d) liste [a, b] --------------------------------------------------------

async def test_list_marker_inside_span_collapses_to_claim_number(tmp_path):
    report = {
        "title": "T",
        "markdown": "Frase una [10, 20].\n\nFrase due.",
        "claims": [
            {"text": "Frase una.", "citation_no": 1, "evidence_ids": ["ev1"]},
            {"text": "Frase due.", "citation_no": 2, "evidence_ids": ["ev2"]},
        ],
    }
    draft = await _write(tmp_path, report)
    assert "Frase una [1]." in draft.markdown
    assert "Frase due [2]." in draft.markdown
    assert "[10, 20]" not in draft.markdown


async def test_list_outside_span_numbers_without_claim_removed(tmp_path):
    # frase non corrispondente ad alcun claim: la lista [2, 99] perde il
    # numero senza claim (99) e tiene solo quello esistente (2)
    report = {
        "title": "T",
        "markdown": "Testo libero [2, 99].\n\nFrase allineata [5].",
        "claims": [
            {"text": "Frase allineata.", "citation_no": 1,
             "evidence_ids": ["ev1"]},
            {"text": "Testo mai scritto.", "citation_no": 2,
             "evidence_ids": ["ev2"]},
        ],
    }
    draft = await _write(tmp_path, report)
    assert "Testo libero [2]." in draft.markdown
    assert "[2, 99]" not in draft.markdown
    assert "99" not in draft.markdown
    # marker sbagliato [5] su frase di claim 1: risolto per posizione
    assert "Frase allineata [1]." in draft.markdown
    assert "[5]" not in draft.markdown


# -- (c) claim senza marker / senza span --------------------------------------

async def test_missing_marker_added_after_sentence(tmp_path):
    report = {
        "title": "T",
        "markdown": "Frase senza marker.",
        "claims": [{"text": "Frase senza marker.", "citation_no": 1,
                    "evidence_ids": ["ev1"]}],
    }
    draft = await _write(tmp_path, report)
    assert draft.markdown.strip() == "Frase senza marker [1]."


async def test_claim_without_span_left_without_marker(tmp_path):
    # la frase del claim 2 non è nel markdown: nessun marker inventato
    report = {
        "title": "T",
        "markdown": "Frase presente.",
        "claims": [
            {"text": "Frase presente.", "citation_no": 1,
             "evidence_ids": ["ev1"]},
            {"text": "Frase assente dal markdown.", "citation_no": 2,
             "evidence_ids": ["ev2"]},
        ],
    }
    draft = await _write(tmp_path, report)
    assert draft.markdown.strip() == "Frase presente [1]."


# -- sanitizzazione: contenuto legit non toccato ------------------------------

async def test_markdown_links_and_images_preserved(tmp_path):
    report = {
        "title": "T",
        "markdown": "Secondo [la fonte](https://x.it) i dati crescono [13].",
        "claims": [{"text": "Secondo [la fonte](https://x.it) i dati crescono.",
                    "citation_no": 1, "evidence_ids": ["ev1"]}],
    }
    draft = await _write(tmp_path, report)
    assert "](https://x.it)" in draft.markdown          # link integro
    assert "i dati crescono [1]." in draft.markdown     # marker allineato


# -- il prompt reale porta le regole CITAZIONI --------------------------------

def test_prompt_contains_citation_rules():
    raw = (_PROMPTS_DIR / "writer.txt").read_text(encoding="utf-8")
    assert "Regole CITAZIONI" in raw
    assert "ev_, clm_, src_" in raw                  # prefissi vietati
    assert "STESSA frase" in raw                     # marker sulla frase giusta
    assert "[3, 7]" in raw                           # lista ammessa, solo numeri
    # lo schema del JSON resta invariato
    assert '"title": str, "markdown": str, "claims"' in raw
