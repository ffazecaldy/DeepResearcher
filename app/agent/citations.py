"""Unified citation numbering (P0-2): one number space for text, claims,
bibliography, UI popover, JSON and PDF.

Contract
--------
- After verification, surviving claims (not REMOVED / FAILED-without-text) are
  renumbered 1..N in order of FIRST appearance in the verified markdown.
- The markdown's citation markers are rewritten to the new numbers. Both
  single [n] and list [n, m] markers are supported; any bracket token that is
  not a comma-separated list of integers (internal ids like [ev_9ab],
  half-written markers like [9b->5ab1469bea24], free text) is removed on
  sight, so the text never carries non-citation brackets.
- POSITION resolution first (default path): a claim whose sentence is located
  in the markdown (tolerant span matching in the style of the verifier's
  _claim_span, implemented locally) OWNS every citation token inside its span
  and those tokens are rewritten to the claim's final number — the in-text
  number is then ALWAYS the number of the right claim, even when the writer
  LLM emitted a misaligned [n] (real-world: sentence carrying [13] for the
  claim numbered 3). The position info is re-derived from the final markdown
  at each call, so no attribute on the claim (e.g. a marker_resolved hint) is
  needed: if the span is found, position resolution is available.
- NUMBER fallback (compat with old runs): markers outside every located span
  are rewritten via the historical draft-number mapping (first-appearance
  order); dangling markers and numbers without a surviving claim are removed.
  When no span is ever located this reduces exactly to the old behavior.
- claim.final_citation_no is the single source of truth for the UI, the JSON
  export and the bibliography builder (orchestrator._final_markdown, NOT this
  module: one Fonti entry per source, at the number of its first citing
  claim). BIBLIOGRAPHY COHERENCE IS GUARANTEED BY CONSTRUCTION: every
  citation token surviving in the text is rewritten to some surviving claim's
  final number, and every surviving claim receives a final number (claims
  without any marker keep their citeable final number, appended in draft
  order). Therefore no in-text number is left without a Fonti entry and no
  Fonti number refers to a number absent from the text.
- Citations pointing at nothing (claim dropped) are removed from the text.
"""
from __future__ import annotations

import re

# single [n] or list [n, m]: the ONLY tokens treated as citation markers
_MARKER = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")
# any single-line bracket token (superset of _MARKER): sanitization candidates
_BRACKET = re.compile(r"\[([^\[\]\n]*)\]")
_TRAILING_PUNCT = re.compile(r"[.!?…]+")


def marker_numbers(token: str) -> list[int]:
    """[7] -> [7]; [10, 20] -> [10, 20] (canonical parse of a marker)."""
    m = _MARKER.fullmatch(token)
    return [int(x) for x in m.group(1).split(",")] if m else []


def format_numbers(nums: list[int]) -> str:
    """[7] or [10, 20] — canonical citation token; dedupe, keep order."""
    uniq = list(dict.fromkeys(nums))
    return "[" + ", ".join(str(n) for n in uniq) + "]" if uniq else ""


def sanitize_brackets(markdown: str) -> str:
    """Remove bracket tokens that are not citation markers.

    Anything that is not a comma-separated list of integers is dropped:
    internal evidence ids ([ev_9ab], [clm_123], [src_4]), half-written
    markers ([9b->5ab1469bea24, vedi claim 16]) and free text. Markdown
    links [label](url) and images ![alt](src) are preserved (their bracket
    belongs to the link syntax, not to a citation).
    """
    pieces: list[str] = []
    last = 0
    for m in _BRACKET.finditer(markdown):
        tok = m.group(0)
        after = markdown[m.end():m.end() + 1]
        before = markdown[m.start() - 1:m.start()]
        if not _MARKER.fullmatch(tok) and after != "(" and before != "!":
            pieces.append(markdown[last:m.start()])
            last = m.end()
    pieces.append(markdown[last:])
    return "".join(pieces)


def _claim_parts(claim_text: str) -> tuple[str, str]:
    """(head, trailing punctuation) of a claim sentence."""
    t = claim_text.strip()
    m = re.match(r"^(.*?)([.!?…]+)$", t, re.DOTALL)
    if m:
        return m.group(1), m.group(2)
    return t, ""


def _span_regexes(claim_text: str) -> list[re.Pattern]:
    """Tolerant span matchers, verifier-_claim_span style but list-aware.

    1st: literal head + optional citation tokens + trailing punctuation
    (fast, precise — the shape the writer emits). 2nd: word-tolerant variant
    that also matches when the writer put citation tokens MID-sentence
    ("Il PVT [13] è alto."). Gaps never cross a line break.
    """
    head, punct = _claim_parts(claim_text)
    if not head.strip():
        return []
    tok = r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]"
    tail_cites = rf"(?: *{tok})*"
    pats = [re.compile(re.escape(head) + rf"\s*{tail_cites}"
                       + (re.escape(punct) if punct else ""))]
    words = [re.escape(w) for w in head.split()]
    if len(words) > 1:
        gap = rf"(?:(?: +|(?:{tok}) *))+"
        pats.append(re.compile(gap.join(words) + tail_cites
                               + (re.escape(punct) if punct else "")))
    return pats


def find_claim_span(markdown: str, claim_text: str,
                    consumed: list[tuple[int, int]] = ()):
    """First match of the claim's sentence not overlapping consumed ranges.

    Returns a re.Match or None. `consumed` lets several claims share one
    markdown without stealing each other's sentence.
    """
    for pat in _span_regexes(claim_text):
        for m in pat.finditer(markdown):
            if any(m.start() < e and s < m.end() for s, e in consumed):
                continue
            return m
    return None


def _rebuild_cluster(span_text: str, marker: str) -> str:
    """Span text with all its citation tokens replaced by ONE marker, placed
    at the end of the sentence (before the trailing punctuation)."""
    t = _MARKER.sub(" ", span_text)
    t = re.sub(r" {2,}", " ", t).strip()
    m = _TRAILING_PUNCT.search(t)
    if m and m.end() == len(t):
        return f"{t[:m.start()].rstrip()} {marker}{t[m.start():]}"
    return f"{t} {marker}".strip()


def _seen_order(markdown: str, claims: list):
    """Shared citation-order computation (used by renumber_citations AND
    source_number_map so the two can never diverge).

    Returns (md, dropped, by_no, spans, tokens, seen): sanitized markdown,
    dropped citation numbers (REMOVED/FAILED claims without replacement
    text), draft-number -> claim map, located claim spans, markdown marker
    tokens and the unified appearance order (located claims by span
    position, markers outside spans by number mapping, remaining claims in
    draft order).
    """
    md = sanitize_brackets(markdown)
    dropped = {c.citation_no for c in claims
               if c.verdict in ("REMOVED", "FAILED")
               and not (c.corrected_text or "").strip()}
    # claim number -> claim (draft numbers identify claims in the text)
    by_no: dict[int, object] = {}
    for c in claims:
        by_no.setdefault(c.citation_no, c)

    # ---- position resolution: each surviving claim owns the tokens inside
    # its span (first non-overlapping match wins; corrected claims are looked
    # up by their corrected text first, then by the original one)
    spans: list[tuple[int, int, object]] = []
    consumed: list[tuple[int, int]] = []
    for c in sorted(claims, key=lambda c: c.citation_no):
        if c.citation_no in dropped:
            continue
        m = None
        if (c.corrected_text or "").strip():
            m = find_claim_span(md, c.corrected_text, consumed)
        if m is None:
            m = find_claim_span(md, c.text, consumed)
        if m is not None:
            spans.append((m.start(), m.end(), c))
            consumed.append((m.start(), m.end()))
    spans.sort(key=lambda t: t[0])

    # ---- unified appearance order: located claims by span position, then
    # markers outside spans by position (number mapping), then the claims
    # without any marker in draft order
    seen: list[object] = [c for _s, _e, c in spans]
    seen_ids = {id(c) for c in seen}
    tokens = list(_MARKER.finditer(md))
    for t in sorted((t for t in tokens
                     if not any(s <= t.start() < e for s, e, _c in spans)),
                    key=lambda t: t.start()):
        for n in marker_numbers(t.group(0)):
            c = by_no.get(n)
            if (c is not None and c.citation_no not in dropped
                    and id(c) not in seen_ids):
                seen.append(c)
                seen_ids.add(id(c))
    for c in sorted(claims, key=lambda c: c.citation_no):
        if c.citation_no not in dropped and id(c) not in seen_ids:
            seen.append(c)
            seen_ids.add(id(c))
    return md, dropped, by_no, spans, tokens, seen


def source_number_map(markdown: str, claims: list,
                      evidences: list) -> dict[str, int]:
    """Per-source citation numbers, SAME unit and order rules as
    renumber_citations: unit = identity_key when present (cross-platform
    copies of one publication collapse into a single number), else
    source_id; numbers assigned at first citation walking claims in
    appearance order. The bibliography builder (orchestrator._final_markdown)
    uses this map so every in-text number has exactly one Fonti entry.
    """
    md, dropped, _by_no, _spans, _tokens, seen = _seen_order(markdown, claims)
    ev_by_id = {e.evidence_id: e for e in evidences}
    source_no: dict[str, int] = {}
    for c in seen:
        if c.citation_no in dropped:
            continue
        for eid in c.evidence_ids:
            e = ev_by_id.get(eid)
            if e is None:
                continue
            k = getattr(e, "identity_key", "") or e.source_id
            if k and k not in source_no:
                source_no[k] = len(source_no) + 1
    return source_no


def renumber_citations(markdown: str, claims: list,
                       evidences: list | None = None) -> tuple[str, list]:
    """Rewrite citation markers and claim numbers into one coherent space.

    Returns (new_markdown, claims_with_final_citation_no). Deterministic.
    Surviving claims are numbered by first appearance in the text — position
    based when their sentence is located (span resolution above), marker
    order otherwise — and markers whose claim no longer exists are dropped.

    SOURCE-BASED numbering (default when `evidences` is given): the number
    space belongs to SOURCES, not to claims. Walking the claims in citation
    order, each source receives its number at its first citation; every
    claim citing it shares that number (claim.final_citation_no = the number
    of its primary source), and a multi-source claim carries all of them
    ([1, 2]). The bibliography builder (orchestrator._final_markdown) emits
    one Fonti entry per source at exactly that number, so by construction
    every in-text number has an entry and no entry refers to an absent
    number — even when several claims cite the same source (the historical
    dangling-[27,28,29] defect).

    CLAIM-BASED fallback (`evidences=None`, compat with old runs): markers
    outside every located span are rewritten via the historical draft-number
    mapping (first-appearance order); dangling markers and numbers without a
    surviving claim are removed. The verifier always passes evidences; the
    fallback keeps the frozen 2-arg call signature working.
    """
    md, dropped, by_no, spans, tokens, seen = _seen_order(markdown, claims)

    def _owned(pos: int) -> bool:
        return any(s <= pos < en for s, en, _c in spans)

    mapping = {c.citation_no: new for new, c in enumerate(seen, start=1)}

    # ---- source-based numbering (evidences given): numbers belong to
    # SOURCES (unit = identity_key when present, source_id otherwise —
    # cross-platform copies of the same publication share ONE number);
    # claims citing the same source share the number, multi-source claims
    # carry [n1, n2]
    if evidences is not None:
        ev_by_id = {e.evidence_id: e for e in evidences}

        def _src_keys(c: object) -> list[str]:
            out: list[str] = []
            for eid in c.evidence_ids:
                e = ev_by_id.get(eid)
                if e is None:
                    continue
                k = getattr(e, "identity_key", "") or e.source_id
                if k and k not in out:
                    out.append(k)
            return out

        source_no: dict[str, int] = {}
        for c in seen:
            for k in _src_keys(c):
                if k not in source_no:
                    source_no[k] = len(source_no) + 1

        def _primary(c: object):
            srcs = _src_keys(c)
            return source_no.get(srcs[0]) if srcs else None

        edits = []
        for s, e, c in spans:
            nums = [source_no[x] for x in _src_keys(c)]
            span_txt = md[s:e]
            if not nums or not _MARKER.search(span_txt):
                # no marker inside the span -> nothing to rewrite: the claim
                # stays marker-less but keeps its final number
                continue
            edits.append((s, e, _rebuild_cluster(span_txt, format_numbers(nums))))
        for t in tokens:
            if _owned(t.start()):
                continue
            new_nums: list[int] = []
            for n in marker_numbers(t.group(0)):
                c = by_no.get(n)
                if c is not None and c.citation_no not in dropped:
                    p = _primary(c)
                    if p is not None and p not in new_nums:
                        new_nums.append(p)
            edits.append((t.start(), t.end(), format_numbers(new_nums)))
    else:
        # ---- legacy claim-number path (2-arg compat for old runs)
        edits = []
        for s, e, c in spans:
            new_no = mapping.get(c.citation_no)
            span_txt = md[s:e]
            if new_no is None or not _MARKER.search(span_txt):
                # no marker inside the span -> nothing to rewrite: the claim
                # stays marker-less but keeps its final number (numbering and
                # bibliography are unaffected). Inserting new markers is the
                # writer's job, not the renumber's.
                continue
            edits.append((s, e, _rebuild_cluster(span_txt, f"[{new_no}]")))
        for t in tokens:
            if _owned(t.start()):
                continue
            new_nums = [mapping[n] for n in marker_numbers(t.group(0))
                        if n in mapping]
            edits.append((t.start(), t.end(), format_numbers(new_nums)))

    out = md
    for s, e, rep in sorted(edits, key=lambda x: x[0], reverse=True):
        out = out[:s] + rep + out[e:]

    # collapse leftover double spaces left by dropped markers
    out = re.sub(r" {2,}", " ", out)
    out = re.sub(r" ([.,;:])", r"\1", out)
    # markers dropped mid-sentence: "X [1]. Y. Z [2]." -> "X [1]. Z [2]."
    out = re.sub(r"\.\s*\.", ".", out)

    for c in claims:
        if c.citation_no in dropped:
            c.final_citation_no = None
        elif evidences is not None:
            c.final_citation_no = _primary(c)
        else:
            c.final_citation_no = mapping.get(c.citation_no)
    return out, claims
