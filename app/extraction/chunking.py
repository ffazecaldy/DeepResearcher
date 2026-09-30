"""Text chunking with paragraph-aware cuts and char overlap.

``chunk_text`` returns Chunk-like tuples ``(char_start, char_end, text)`` where
``char_start``/``char_end`` index the ORIGINAL text and ``text`` is exactly
``original[char_start:char_end]`` (spans stay verbatim for evidence quoting).
"""
from __future__ import annotations

# Cut-point separators, best first: paragraph break > sentence end > space.
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", "! ", "? ", " ")


def chunk_text(
    text: str, max_chars: int = 4000, overlap: int = 200
) -> list[tuple[int, int, str]]:
    """Split ``text`` into chunks of at most ``max_chars`` chars.

    Cuts at paragraph boundaries when possible (then sentence ends, then
    spaces; a hard cut only as last resort). Consecutive chunks share ~
    ``overlap`` chars. Always makes progress; whitespace-only chunks skipped.
    """
    if not isinstance(text, str) or not text:
        return []
    max_chars = max(1, int(max_chars))
    # Overlap must stay strictly smaller than a chunk for progress guarantees.
    overlap = max(0, min(int(overlap), max_chars // 2))

    n = len(text)
    chunks: list[tuple[int, int, str]] = []
    cursor = 0
    while cursor < n:
        end_target = min(cursor + max_chars, n)
        cut = _find_cut(text, cursor, end_target)
        if text[cursor:cut].strip():
            chunks.append((cursor, cut, text[cursor:cut]))
        if cut >= n:
            break
        cursor = _next_start(text, cut, overlap)
    return chunks


def _find_cut(text: str, start: int, end: int) -> int:
    """Cut point in ``(start, end]``; prefer a boundary in the last 3/4 of the window."""
    if end >= len(text):
        return end
    lo = start + max(1, (end - start) // 4)
    for sep in _SEPARATORS:
        idx = text.rfind(sep, lo, end)
        if idx != -1:
            return min(end, idx + len(sep))
    return end  # hard cut: no usable boundary in the window


def _next_start(text: str, cut: int, overlap: int) -> int:
    """Resume ``overlap`` chars before ``cut``, snapped forward to a word start."""
    nxt = max(cut - overlap, 1) if overlap > 0 else cut
    if nxt >= cut:
        nxt = cut
    else:
        while nxt < cut and not text[nxt].isspace():
            nxt += 1
        while nxt < cut and text[nxt].isspace():
            nxt += 1
        nxt = min(nxt, cut)
    return max(nxt, cut and min(cut, len(text)) and 1) if False else max(nxt, 1)
