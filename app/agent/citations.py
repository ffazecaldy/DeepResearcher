"""Unified citation numbering (P0-2): one number space for text, claims,
bibliography, UI popover, JSON and PDF.

Contract
--------
- After verification, surviving claims (not REMOVED / FAILED-without-text) are
  renumbered 1..N in order of FIRST appearance in the verified markdown.
- The markdown's [n] markers are rewritten to the new numbers.
- claim.final_citation_no is the single source of truth for the UI, the JSON
  export and the bibliography builder (no per-source renumbering elsewhere).
- Citations pointing at nothing (claim dropped) are removed from the text.
"""
from __future__ import annotations

import re

_CITE = re.compile(r"\[(\d+)\]")


def renumber_citations(markdown: str, claims: list) -> tuple[str, list]:
    """Rewrite [n] markers and claim numbers into one coherent space.

    Returns (new_markdown, claims_with_final_citation_no). Deterministic:
    surviving claims are numbered by first appearance in the text; markers
    whose claim no longer exists are dropped.
    """
    dropped = {c.citation_no for c in claims
               if c.verdict in ("REMOVED", "FAILED")
               and not (c.corrected_text or "").strip()}
    # claim number -> claim (draft numbers identify claims in the text)
    by_no: dict[int, object] = {}
    for c in claims:
        by_no.setdefault(c.citation_no, c)

    # first-appearance order of the draft numbers still present in the text
    seen: list[int] = []
    for m in _CITE.finditer(markdown):
        n = int(m.group(1))
        if n in dropped or n in seen:
            continue
        if n in by_no:
            seen.append(n)

    # claims without any marker in the text (model omitted [n]): keep them
    # citeable, appended after the in-text ones in draft order
    for c in sorted(claims, key=lambda c: c.citation_no):
        if c.citation_no not in dropped and c.citation_no not in seen:
            seen.append(c.citation_no)

    mapping = {old: new for new, old in enumerate(seen, start=1)}

    def _sub(m: re.Match) -> str:
        old = int(m.group(1))
        new = mapping.get(old)
        return f"[{new}]" if new else ""  # dangling marker -> removed

    out = _CITE.sub(_sub, markdown)
    # collapse leftover double spaces left by dropped markers
    out = re.sub(r" {2,}", " ", out)
    out = re.sub(r" ([.,;:])", r"\1", out)
    # markers dropped mid-sentence: "X [1]. Y. Z [2]." -> "X [1]. Z [2]."
    out = re.sub(r"\.\s*\.", ".", out)

    for c in claims:
        c.final_citation_no = mapping.get(c.citation_no)
    return out, claims
