"""Cheap Italian-vs-other language check for the final report (no deps).

Heuristic: counts Italian vs English function words over normalized tokens.
Good enough to catch a report written in the wrong language; it is NOT a
linguistic analyser.
"""
from __future__ import annotations

import re

_IT_WORDS = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "del", "della",
    "dei", "delle", "che", "per", "con", "su", "sono", "essere", "ha", "hanno",
    "nel", "nella", "sul", "sulla", "come", "dove", "quando", "piu", "anche",
    "pero", "quindi", "secondo", "tra", "fra", "si", "non", "ciò", "questo",
    "questa", "risultati", "fonti", "secondo",
}
_EN_WORDS = {
    "the", "and", "of", "to", "is", "are", "was", "were", "with", "for", "that",
    "this", "it", "as", "by", "from", "has", "have", "had", "on", "at", "be",
    "been", "which", "their", "there", "about", "into", "than", "then", "when",
    "while", "also", "will", "within", "all", "not", "an", "its", "or", "can",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zà-ù']+", text.lower(), re.UNICODE)


def looks_italian(text: str, min_tokens: int = 40) -> bool:
    """True when English function words do NOT dominate Italian ones."""
    toks = _tokens(text)
    if len(toks) < min_tokens:
        return True  # too short to judge (e.g. title-only): don't fail
    it = sum(1 for t in toks if t in _IT_WORDS)
    en = sum(1 for t in toks if t in _EN_WORDS)
    if en == 0:
        return True
    if it == 0:
        return False
    return it / en >= 0.5  # italian must reach at least half the english count
