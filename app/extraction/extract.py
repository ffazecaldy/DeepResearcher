"""HTML main-content extraction with metadata.

Primary: trafilatura (bare_extraction with metadata).
Fallback: readability-lxml when the trafilatura text is missing or shorter
than ``_FALLBACK_THRESHOLD`` chars. Never raises: on total failure returns
``("", meta)`` with at least the title from ``<title>``.
"""
from __future__ import annotations

import html as _html
import logging
import re
from typing import Any

import trafilatura

log = logging.getLogger(__name__)

# Below this length the readability-lxml fallback is attempted (spec: 200).
_FALLBACK_THRESHOLD = 200

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def extract_html(html: str, url: str | None = None) -> tuple[str, dict[str, Any]]:
    """Extract main text and metadata from an HTML string.

    Returns ``(text, meta)`` where ``meta = {"title", "author", "published_at"}``
    (values are ``str | None``). Never raises.
    """
    meta: dict[str, Any] = {"title": None, "author": None, "published_at": None}
    if not isinstance(html, str) or not html.strip():
        return "", meta

    # Baseline title straight from <title>; overridden by richer metadata below.
    meta["title"] = _title_from_source(html)

    text = _via_trafilatura(html, url, meta)
    if text is None or len(text) < _FALLBACK_THRESHOLD:
        alt = _via_readability(html, meta)
        if alt and (text is None or len(alt) > len(text)):
            text = alt

    if not text:
        return "", meta
    return _tidy(text), meta


def _via_trafilatura(
    html: str, url: str | None, meta: dict[str, Any]
) -> str | None:
    try:
        doc = trafilatura.bare_extraction(
            html, url=url, with_metadata=True, output_format="python"
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        log.debug("trafilatura failed on %s: %s", url or "<inline>", exc)
        return None
    if doc is None:
        return None

    if hasattr(doc, "as_dict"):
        data = doc.as_dict()
    elif isinstance(doc, dict):
        data = doc
    else:  # unexpected shape: pull the known attributes defensively
        data = {k: getattr(doc, k, None) for k in ("text", "title", "author", "date")}

    if meta["title"] is None and data.get("title"):
        meta["title"] = _clean(data["title"])
    if data.get("author"):
        meta["author"] = _clean(data["author"])
    if data.get("date"):
        meta["published_at"] = _clean(data["date"])
    return (data.get("text") or "").strip() or None


def _via_readability(html: str, meta: dict[str, Any]) -> str | None:
    try:
        import readability

        doc = readability.Document(html)
        summary = doc.summary(html_partial=False)
        text = _strip_tags(summary).strip()
        if not text:
            return None
        if meta["title"] is None:
            try:
                meta["title"] = _clean(doc.short_title())
            except Exception:  # noqa: BLE001
                pass
        return text
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        log.debug("readability fallback failed: %s", exc)
        return None


def _title_from_source(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    if not m:
        return None
    return _clean(_html.unescape(_strip_tags(m.group(1))))


def _strip_tags(fragment: str) -> str:
    try:
        from lxml import html as lxml_html

        return lxml_html.fromstring(fragment).text_content()
    except Exception:  # noqa: BLE001 - malformed fragment: regex fallback
        return _html.unescape(_TAG_RE.sub(" ", fragment))


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = _SPACES_RE.sub(" ", str(value)).strip()
    return s or None


def _tidy(text: str) -> str:
    """Collapse horizontal whitespace per line; keep paragraph newlines."""
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
