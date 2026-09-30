"""PDF text extraction via pypdf. Never raises: errors yield ``None``."""
from __future__ import annotations

import io
import logging

import pypdf

log = logging.getLogger(__name__)


def extract_pdf(data: bytes) -> str | None:
    """Extract text from PDF bytes; return ``None`` on any failure or empty text."""
    if not data:
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - skip broken page, keep the rest
                continue
        text = "\n".join(parts).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        log.debug("pdf extraction failed (%d bytes): %s", len(data), exc)
        return None
