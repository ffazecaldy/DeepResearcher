"""P1-8: snowballing works on REAL <a href> links from the raw HTML, and the
discovered URL is fetched directly (never sent to the search engine)."""
from __future__ import annotations

import json

import pytest

from app.agent.snowball import SnowballState, extract_links


HTML = """
<html><body>
<article>
Una scoperta rilevante documentata nello <a href="https://primary.example/studio">studio originale</a>.
Vedi anche <a href="/relativo">pagina interna</a> e <a href="mailto:x@y.z">mail</a>.
</article>
</body></html>
"""


def test_extract_links_from_raw_html_finds_primary_source():
    links = extract_links(HTML, base_url="https://blog.example/post",
                          max_links=10)
    assert "https://primary.example/studio" in links
    assert "https://blog.example/relativo" in links  # relative resolved
    assert all(not u.startswith("mailto:") for u in links)


def test_extract_links_from_plain_text_returns_nothing():
    # the OLD behavior: extracted text (no <a>) yielded zero links
    assert extract_links("testo senza tag, https://plain.example/ref") == []


def test_snowball_state_proposes_and_dedupes():
    st = SnowballState()
    st.propose("https://primary.example/studio")
    st.propose("https://primary.example/studio")  # duplicate
    st.propose("https://primary.example/altro")
    pending = st.take_pending(already_seen_norm=set())
    assert len(pending) == 2  # duplicate proposal collapsed
    # take_pending is non-consuming (the orchestrator dedupes via seen_urls):
    # the same candidates return until marked seen
    assert len(st.take_pending(already_seen_norm=set())) == 2
    seen = {pending[0]}
    assert st.take_pending(already_seen_norm=seen) == [pending[1]]


def test_snowball_skips_already_seen_urls():
    st = SnowballState()
    st.propose("https://seen.example/a")
    out = st.take_pending(already_seen_norm={"https://seen.example/a"})
    assert out == []
