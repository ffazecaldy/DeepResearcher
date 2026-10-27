"""Regression P0-4 (reader): quote di sostanza + scope popolazione.

- quote solo-titolo (breve, isolata, senza verbo/punto o = titolo doc) scartata
  e scarto loggato
- quote breve ma incorporata in una frase piena: resta valida (no over-firing)
- il claim non puo' allargare la popolazione della quote: quote su "ADHD in
  bambini e adolescenti" + claim generico -> evidenza scartata (log)
- claim che mantiene la popolazione (restringe): resta valido
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.agent.reader import Reader
from app.models import Chunk
from tests.fakes import FakeLLMClient

BODY = ("Uno studio randomizzato ha concluso che il trattamento non ha "
        "migliorato i sintomi core ADHD in bambini e adolescenti. "
        "Altri studi sono in corso.")
# frase piena con data incorporata (per il no-over-firing del titolo)
TEXT_BELL = ("Alexander Graham Bell brevettò il telefono nel 1876, mentre "
             "l'Italia ricorda Antonio Meucci come pioniere. La prima "
             "chiamata avvenne il 10 marzo 1876.")


def _chunk(text: str = BODY) -> Chunk:
    return Chunk(source_id="src1", idx=0, text=text,
                 char_start=0, char_end=len(text))


def _tmp_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "reader.txt").write_text(
        "SYS\n---SYSTEM---\nQ:{question} SQ:{subquestion} EXT:{external_content}\n",
        encoding="utf-8")
    return d


def _resp(quote: str, claim: str) -> str:
    return json.dumps([{"quote": quote, "claim": claim}], ensure_ascii=False)


async def test_title_only_quote_dropped_and_logged(tmp_path, caplog):
    """Quote = riga isolata breve senza verbo ne' punto (= intestazione) -> scarto."""
    page = "Nuovi risultati sull'omega-3\n\n" + BODY
    reader = Reader(FakeLLMClient([_resp("Nuovi risultati sull'omega-3",
                                         "Risultati nuovi")]),
                    prompts_dir=_tmp_prompts(tmp_path))
    with caplog.at_level(logging.INFO, logger="app.agent.reader"):
        evs = await reader.extract_evidences("q", "sq", _chunk(page))
    assert evs == []
    assert any("scartata quote-titolo" in rec.message for rec in caplog.records)


async def test_quote_equal_to_doc_title_dropped(tmp_path):
    """Quote coincidente col titolo del documento -> scarto (anche con punto)."""
    title = "Lo studio non ha migliorato i sintomi core ADHD."
    page = f"{title}\n\nDettagli metodologici: 200 partecipanti, dodici settimane."
    reader = Reader(FakeLLMClient([_resp(title, "Nessun miglioramento ADHD")]),
                    prompts_dir=_tmp_prompts(tmp_path))
    evs = await reader.extract_evidences("q", "sq", _chunk(page),
                                         doc_title=title)
    assert evs == []


async def test_short_embedded_quote_still_valid(tmp_path):
    """Quote breve MA incorporata in una frase piena: NON e' un titolo."""
    reader = Reader(FakeLLMClient([_resp("il 10 marzo 1876",
                                         "prima chiamata il 10 marzo 1876")]),
                    prompts_dir=_tmp_prompts(tmp_path))
    evs = await reader.extract_evidences("q", "sq", _chunk(TEXT_BELL))
    assert len(evs) == 1


async def test_population_scope_widening_dropped_and_logged(tmp_path, caplog):
    """Quote su ADHD/bambini/adolescenti + claim generico -> evidenza scartata."""
    quote = ("il trattamento non ha migliorato i sintomi core ADHD in "
             "bambini e adolescenti")
    reader = Reader(FakeLLMClient([_resp(quote,
                                         "L'integrazione migliora i sintomi")]),
                    prompts_dir=_tmp_prompts(tmp_path))
    with caplog.at_level(logging.INFO, logger="app.agent.reader"):
        evs = await reader.extract_evidences("q", "sq", _chunk())
    assert evs == []
    assert any("allarga la popolazione" in rec.message
               for rec in caplog.records)


async def test_population_kept_in_claim_valid(tmp_path):
    """Claim che mantiene la popolazione della quote (restringe): valido."""
    quote = ("il trattamento non ha migliorato i sintomi core ADHD in "
             "bambini e adolescenti")
    reader = Reader(FakeLLMClient([_resp(
        quote,
        "Nei bambini con ADHD il trattamento non ha migliorato i sintomi")]),
        prompts_dir=_tmp_prompts(tmp_path))
    evs = await reader.extract_evidences("q", "sq", _chunk())
    assert len(evs) == 1
    assert evs[0].claim.startswith("Nei bambini con ADHD")
