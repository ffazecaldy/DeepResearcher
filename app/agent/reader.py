"""Reader: Chunk -> verified Evidence list (quotes must exist in the chunk)."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

from app.models import Chunk, Evidence
from app.security import wrap_external

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

# directional/typographic chars -> ASCII equivalents, for lenient quote matching
_QUOTE_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2039": "'", "\u203a": "'",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
}


def _load_template(prompts_dir: Path, name: str) -> tuple[str, str]:
    raw = (prompts_dir / name).read_text(encoding="utf-8")
    if "---SYSTEM---" not in raw:
        raise ValueError(f"prompt file {name} missing ---SYSTEM--- marker")
    system, user = raw.split("---SYSTEM---", 1)
    return system.strip(), user.strip()


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


def _extract_balanced(t: str, start: int, open_ch: str, close_ch: str) -> str:
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(t[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    raise ValueError(f"unbalanced {open_ch}{close_ch} block")


def _extract_json(text: str):
    t = _strip_json_fence(text)
    arr = t.find("[")
    obj = t.find("{")
    if arr != -1 and (obj == -1 or arr < obj):
        return json.loads(_extract_balanced(t, arr, "[", "]"))
    if obj != -1:
        return json.loads(_extract_balanced(t, obj, "{", "}"))
    raise ValueError("no JSON found")


def _normalize_text(s: str) -> str:
    """Fold to comparable content chars: drop whitespace and mapped punctuation."""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if ch not in _QUOTE_MAP and not ch.isspace())
    return s.lower()


def _content_indices(text: str) -> list[int]:
    """Indices of the chars _normalize_text keeps, in original-text order."""
    return [i for i, ch in enumerate(text)
            if ch not in _QUOTE_MAP and not ch.isspace()]


def _find_span(quote: str, chunk_text: str) -> tuple[int, int] | None:
    """Locate quote in chunk.text: exact first, then normalized (ws/case/quotes)."""
    if not quote:
        return None
    idx = chunk_text.find(quote)
    if idx != -1:
        return idx, idx + len(quote)

    n_chunk = _normalize_text(chunk_text)
    n_quote = _normalize_text(quote)
    if not n_quote:
        return None
    pos = n_chunk.find(n_quote)
    if pos == -1:
        return None

    idxs = _content_indices(chunk_text)
    if not idxs or pos >= len(idxs):
        return None
    start = idxs[pos]
    end_off = pos + len(n_quote) - 1
    if end_off >= len(idxs):
        return None
    return start, idxs[end_off] + 1


class Reader:
    def __init__(self, llm, prompts_dir: Path | None = None):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR

    async def extract_evidences(self, question: str, subquestion_text: str,
                                chunk: Chunk, max_evidences: int = 5,
                                subquestion_id: str | None = None) -> list[Evidence]:
        """Extract quotes+claims from one chunk; unverifiable quotes are dropped.

        LLM/JSON failures log a warning and return [] instead of raising.
        """
        try:
            system_tpl, user_tpl = _load_template(self._prompts_dir, "reader.txt")
        except (ValueError, OSError) as exc:
            log.error("reader: cannot load reader.txt: %s", exc)
            return []

        user = user_tpl.format(
            question=question,
            subquestion=subquestion_text,
            external_content=wrap_external(chunk.text),
        )
        lang_rule = (f"REGOLA: il campo 'claim' deve essere in italiano "
                     f"(traduci i concetti); 'quote' resta SEMPRE nella lingua "
                     f"originale del testo, copiata letteralmente.")
        user = f"{lang_rule}\n\n{user}"

        try:
            raw = await self._llm.complete(system_tpl, user, force_json=True)
            items = _extract_json(raw)
        except Exception as exc:  # LLM/network/JSON: never crash the pipeline
            log.warning("reader: LLM call failed on chunk %s: %s",
                        chunk.chunk_id, exc)
            return []

        if isinstance(items, dict):
            for key in ("evidences", "evidence", "items", "results"):
                if isinstance(items.get(key), list):
                    items = items[key]
                    break
            else:
                items = []
        if not isinstance(items, list):
            items = []

        evidences: list[Evidence] = []
        for item in items:
            if len(evidences) >= max_evidences:
                break
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote", "")).strip()
            claim = str(item.get("claim", "")).strip()
            if not quote or not claim:
                continue
            span = _find_span(quote, chunk.text)
            if span is None:
                log.debug("reader: dropped unverifiable quote on chunk %s: %r",
                          chunk.chunk_id, quote[:80])
                continue
            evidences.append(Evidence(
                source_id=chunk.source_id,
                chunk_id=chunk.chunk_id,
                quote=chunk.text[span[0]:span[1]],
                span_start=span[0],
                span_end=span[1],
                claim=claim,
                subquestion_id=subquestion_id,
            ))
        return evidences
