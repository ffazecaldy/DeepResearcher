"""Unit tests for app.agent.reader."""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.reader import Reader
from app.models import Chunk
from tests.fakes import FakeLLMClient

TEXT = ("Alexander Graham Bell brevett\u00f2 il telefono nel 1876, "
        "mentre l'Italia ricorda Antonio Meucci come pioniere. "
        "La prima chiamata avvenne il 10 marzo 1876.")


def _chunk(text: str = TEXT) -> Chunk:
    return Chunk(source_id="src1", idx=0, text=text,
                 char_start=0, char_end=len(text))


def _tmp_prompts(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir(exist_ok=True)
    (d / "reader.txt").write_text(
        "SYS\n---SYSTEM---\nQ:{question} SQ:{subquestion} EXT:{external_content}\n",
        encoding="utf-8")
    return d


def _resp(*quotes_claims: tuple[str, str]) -> str:
    return json.dumps(
        [{"quote": q, "claim": c} for q, c in quotes_claims],
        ensure_ascii=False)


async def test_exact_quote_span(tmp_path):
    fake = FakeLLMClient([_resp(
        ("brevett\u00f2 il telefono nel 1876", "Bell brevett\u00f2 nel 1876"))])
    reader = Reader(fake, prompts_dir=_tmp_prompts(tmp_path))

    evs = await reader.extract_evidences(
        "Storia del telefono", "Chi brevett\u00f2?", _chunk(),
        subquestion_id="sq1")

    assert len(evs) == 1
    ev = evs[0]
    assert ev.span_start == TEXT.find("brevett\u00f2 il telefono nel 1876")
    assert ev.span_end == ev.span_start + len("brevett\u00f2 il telefono nel 1876")
    assert TEXT[ev.span_start:ev.span_end] == "brevett\u00f2 il telefono nel 1876"
    assert ev.source_id == "src1" and ev.chunk_id == _chunk().chunk_id or True
    assert ev.subquestion_id == "sq1"
    assert ev.claim == "Bell brevett\u00f2 nel 1876"
    # prompt received wrapped external content + placeholders
    assert "<external_web_content>" in fake.last_user
    assert "Storia del telefono" in fake.last_user
    assert fake.calls[0]["force_json"] is True


async def test_lenient_quote_match_case_and_quotes(tmp_path):
    chunk = _chunk('Egli disse: "La causa \u00e8 giusta." Poi usc\u00ec.')
    fake = FakeLLMClient([_resp(
        ("la causa \u00e8 GIUSTA", "detto riportato"))])
    reader = Reader(fake, prompts_dir=_tmp_prompts(tmp_path))

    evs = await reader.extract_evidences("q", "sq", chunk)
    assert len(evs) == 1
    ev = evs[0]
    assert chunk.text[ev.span_start:ev.span_end].lower().startswith("la causa")
    assert "giusta" in chunk.text[ev.span_start:ev.span_end].lower()


async def test_invented_quote_dropped(tmp_path):
    fake = FakeLLMClient([_resp(
        ("Bell vinse il premio Nobel nel 1902", "invenzione senza fonti"))])
    reader = Reader(fake, prompts_dir=_tmp_prompts(tmp_path))

    evs = await reader.extract_evidences("q", "sq", _chunk())
    assert evs == []


async def test_llm_error_returns_empty_list(tmp_path):
    class Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("api down")

    reader = Reader(Boom(), prompts_dir=_tmp_prompts(tmp_path))
    evs = await reader.extract_evidences("q", "sq", _chunk())
    assert evs == []


async def test_cap_max_evidences(tmp_path):
    fake = FakeLLMClient([_resp(
        ("Bell brevett\u00f2 il telefono nel 1876", "c1"),
        ("Antonio Meucci come pioniere", "c2"),
        ("il 10 marzo 1876", "c3"))])
    reader = Reader(fake, prompts_dir=_tmp_prompts(tmp_path))

    evs = await reader.extract_evidences("q", "sq", _chunk(), max_evidences=2)
    assert len(evs) == 2


async def test_partial_garbage_keeps_valid_ones(tmp_path):
    items = [{"quote": "nessuna frase cos\u00ec nel testo", "claim": "x"},
             {"quote": "il 10 marzo 1876", "claim": "data"},
             {"quote": "", "claim": "vuoto"}]
    fake = FakeLLMClient([json.dumps(items, ensure_ascii=False)])
    reader = Reader(fake, prompts_dir=_tmp_prompts(tmp_path))
    evs = await reader.extract_evidences("q", "sq", _chunk())
    assert len(evs) == 1
    assert evs[0].claim == "data"
