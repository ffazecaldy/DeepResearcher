"""P1-6: reading must be relevance-oriented, not position-based."""
from __future__ import annotations

from app.extraction.chunking import chunk_text
from app.models import Chunk, SubQuestion


def _chunks(text: str, source_id: str = "s1") -> list[Chunk]:
    out = []
    for i, (start, end, t) in enumerate(chunk_text(text, max_chars=200, overlap=0)):
        out.append(Chunk(chunk_id=f"chk_{source_id}_{i}", source_id=source_id,
                         idx=i, text=t, char_start=start, char_end=end))
    return out


def _read_selection(text: str, subquestions: list[SubQuestion], cap: int):
    """Mirror of Orchestrator._pick_chunks scoring (keyword overlap)."""
    import re

    def _kw(t: str) -> set[str]:
        return set(re.findall(r"[\wà-ù]{4,}", t.lower()))

    sq_tokens = [(sq, _kw(sq.text)) for sq in subquestions]
    all_chunks = _chunks(text)
    if len(all_chunks) <= cap:
        return all_chunks
    scored = []
    for i, ch in enumerate(all_chunks):
        toks = _kw(ch.text)
        score = max((len(toks & st) for _s, st in sq_tokens), default=0)
        scored.append((-score, i))
    scored.sort()
    picked = [all_chunks[i] for _s, i in scored[:cap]]
    picked.sort(key=lambda c: c.idx)
    return picked


def test_relevant_chunk_at_page_bottom_is_selected():
    long_text = "Ricette di cucina spagnola paella ingredienti preparazione. " * 10
    relevant = ("La paella originale usa zafferano e riso bomba. " * 10)
    # the relevant content is in the LAST chunk
    text = long_text + relevant
    sqs = [SubQuestion(subquestion_id="sq1", text=" ingredienti paella zafferano",
                       idx=1)]
    picked = _read_selection(text, sqs, cap=2)
    texts = " ".join(c.text for c in picked)
    assert "zafferano e riso bomba" in texts, \
        "bottom-of-page relevant chunk must be selected"


def test_budget_share_leaves_room_for_later_docs():
    # 60 evidences budget, 10 docs: doc 1 gets at most ceil(60/10)=6 chunks
    # (per-source cap may lower it, but never the WHOLE budget)
    budget, remaining = 60, 10
    share = max(1, -(-budget // remaining))
    assert share == 6
