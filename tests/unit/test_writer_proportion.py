"""PROPORZIONE writer: regole nel prompt, hedged facoltativo, markdown pulito.

Offline: FakeLLMClient + prompt temporanei (tranne il test sul contenuto del
prompt reale, che legge solo file locali).
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


def _ev(eid: str = "ev1") -> Evidence:
    return Evidence(evidence_id=eid, source_id="src1", chunk_id="chk1",
                    quote="quote", claim="claim", run_id="r1")


def _subs() -> list[SubQuestion]:
    return [SubQuestion(subquestion_id="sq1", text="Chi?", idx=1)]


# -- 1) il prompt reale contiene le regole di proporzione -------------------

def test_prompt_contains_proportion_rules():
    raw = (_PROMPTS_DIR / "writer.txt").read_text(encoding="utf-8")
    # regole di proporzione richieste
    assert "PROPORZIONE" in raw
    assert "la recensione consiglia il libro" in raw
    assert "Il più consigliato" in raw and "CONFRONTO" in raw
    assert "EFFETTO" in raw and "migliora la concentrazione" in raw
    assert "secondo X" in raw  # giudizi soggettivi attribuiti, mai fatti universali
    assert "versione proporzionata" in raw  # proporzionata + segnala il limite
    # no asterischi grezzi malformati: il corsivo e' del renderer
    assert "asterischi grezzi malformati" in raw
    # schema esistente invariato: title/markdown/claims restano
    assert '"title": str, "markdown": str, "claims"' in raw
    # hedged e' per-claim e FACOLTATIVO
    assert '"hedged": bool' in raw and "FACOLTATIVO" in raw


async def test_user_prompt_actually_carries_hedged(tmp_path):
    report = {"title": "T", "markdown": "Frase.",
              "claims": [{"text": "Frase.", "evidence_ids": ["ev1"]}]}
    fake = FakeLLMClient([json.dumps(report)])
    w = Writer(fake)  # prompt reale di default
    await w.write("domanda", "it", _subs(), [_ev()])
    assert "hedged" in fake.last_user
    assert "PROPORZIONE" in fake.last_system


# -- 2) hedged facoltativo non rompe il parsing (assenza = False) -----------

async def test_hedged_parsed_optional_absence_is_false(tmp_path):
    report = {
        "title": "T",
        "markdown": "Frase uno.\n\nFrase due.",
        "claims": [
            {"text": "Frase uno.", "citation_no": 1, "evidence_ids": ["ev1"],
             "hedged": True},
            {"text": "Frase due.", "citation_no": 2, "evidence_ids": ["ev1"]},
        ],
    }
    fake = FakeLLMClient([json.dumps(report)])
    w = Writer(fake, prompts_dir=_tmp_prompts(tmp_path))

    draft = await w.write("domanda", "it", _subs(), [_ev()])

    assert len(draft.claims) == 2
    assert draft.claims[0].hedged is True      # presente e vero
    assert draft.claims[1].hedged is False     # assente -> false


async def test_hedged_non_bool_value_does_not_break_parsing(tmp_path):
    report = {
        "title": "T",
        "markdown": "Frase.",
        "claims": [
            {"text": "Frase.", "citation_no": 1, "evidence_ids": ["ev1"],
             "hedged": "false"},   # stringa, non bool
        ],
    }
    fake = FakeLLMClient([json.dumps(report)])
    w = Writer(fake, prompts_dir=_tmp_prompts(tmp_path))

    draft = await w.write("domanda", "it", _subs(), [_ev()])

    assert len(draft.claims) == 1
    assert draft.claims[0].hedged is False


# -- 3) markdown: no asterischi grezzi malformati ---------------------------

async def test_malformed_asterisks_stripped_valid_emphasis_kept(tmp_path):
    md = ("La recensione *consiglia il libro.\n\n"
          "**Punto forte** e *corsivo ok*.\n\n"
          "***\n\n"
          "Secondo la recensione, il libro è valido.")
    report = {
        "title": "T",
        "markdown": md,
        "claims": [{"text": "Frase.", "citation_no": 1, "evidence_ids": ["ev1"]}],
    }
    fake = FakeLLMClient([json.dumps(report)])
    w = Writer(fake, prompts_dir=_tmp_prompts(tmp_path))

    draft = await w.write("domanda", "it", _subs(), [_ev()])

    assert "La recensione consiglia il libro." in draft.markdown
    assert "*" not in draft.markdown.splitlines()[0]          # sbilanciato rimosso
    assert "**Punto forte** e *corsivo ok*." in draft.markdown  # enfasi valida resta
    assert "***" in draft.markdown                              # separatore resta
    assert "Secondo la recensione" in draft.markdown
