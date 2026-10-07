"""Text -> claim coverage check (P0-3): every factual sentence in the final
report must be backed by a verified claim. Sentences not covered are flagged
inline instead of passing silently.

Heuristic, deliberately conservative: only sentences that LOOK factual
(contain a number, a year, a percentage, or a modal of assertion) and are NOT
part of any claim (or its corrected text) get flagged. Narrative/transitional
sentences are left alone to avoid noise.
"""
from __future__ import annotations

import re

_FACTUAL = re.compile(
    r"\d|%\b|\bsecondo\b|\bdimostr\w+|\bstudi\b|\banalisi\b|\bdati\b", re.IGNORECASE)
_SKIP = re.compile(
    r"^[>\-*#]|\]|^\s*$|\bNota\b|non verificata|Metodo e limiti", re.IGNORECASE)


def _sentences(markdown: str) -> list[tuple[int, str, int]]:
    """(line_index, sentence, char_offset_in_line) factual-looking sentences."""
    out = []
    for li, line in enumerate(markdown.splitlines()):
        if _SKIP.search(line):
            continue
        pos = 0
        for s in re.split(r"(?<=[.!?])\s+", line):
            if s.strip() and _FACTUAL.search(s):
                out.append((li, s.strip(), pos))
            pos += len(s) + 1
    return out


def _norm(s: str) -> str:
    s = re.sub(r"\[\d+\]", " ", s)
    s = re.sub(r"[^\w\sà-ù]", " ", s)
    return " ".join(s.lower().split())


def uncovered_sentences(markdown: str, covered_texts: set[str]) -> list[str]:
    """Factual sentences not matched (normalized substring) by any claim text."""
    covered_norm = [_norm(t) for t in covered_texts if t and len(t) > 10]
    out = []
    for _li, s, _pos in _sentences(markdown):
        ns = _norm(s)
        if len(ns) < 15:
            continue
        if any(ns in cn or cn in ns for cn in covered_norm):
            continue
        out.append(s)
    return out


def mark_uncovered(markdown: str, uncovered: list[str]) -> str:
    """Append a visible warning to each uncovered sentence (never silent)."""
    for s in uncovered:
        if s in markdown:
            markdown = markdown.replace(
                s, s + " ⚠ *non coperta da alcuna evidenza verificata*", 1)
    return markdown
