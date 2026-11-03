"""B20b: segnale ABSTRACT-ONLY per le fonti.

Missione punto 4: "Se è disponibile solo l'abstract, segnalalo."

- euristica `abstract_only_from` (testo breve + meta citation_abstract/
  dc.description nel <head> oppure riga "Abstract" da sola, stile PubMed);
- un testo lungo non è MAI abstract-only, qualunque meta ci sia;
- il flag viaggia come attributo dinamico del fetcher (convenzione
  identity_key), sopravvive al cache roundtrip e viene persistito su
  sources.abstract_only via MIGRAZIONE (PRAGMA guard) per i DB vecchi.

Offline: server HTTP locale, zero rete esterna, zero API key.
"""
from __future__ import annotations

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agent.fetcher import Fetcher
from app.extraction.extract import (ABSTRACT_ONLY_MAX_CHARS, abstract_only_from,
                                    extract_html)
from app.limits import DiskCache
from app.models import SearchResultItem
from app.storage import Storage

_AB_BODY = ("Background: studio osservazionale su 240 adulti. ") * 30
PUBMED_ABSTRACT_HTML = (
    "<html><head><title>Studio X</title>"
    '<meta name="citation_title" content="Studio X"></head><body>'
    "<h2>Abstract</h2>"
    f"<p>{_AB_BODY}</p>"
    '<div class="references">Benjamini Y, Hochberg Y. Controlling the false '
    "discovery rate. J R Stat Soc B 1995;57(1):289-300.</div></body></html>")

OJS_HTML = (
    "<html><head><title>Articolo</title>"
    '<meta name="dc.description" content="Riassunto dello studio."></head>'
    "<body><p>Pagina di landing della rivista con solo il riassunto.</p>"
    "</body></html>")

FULL_TEXT_HTML = (
    "<html><head><title>Full text</title>"
    '<meta name="citation_abstract" content="abs"></head><body><p>'
    + ("Testo integrale con metodi, risultati e discussione. " * 400)
    + "</p></body></html>")

CALL_FOR_PAPERS_HTML = (
    "<html><head><title>Conferenza</title></head><body>"
    "<h1>Call for abstracts</h1><p>InvioProposta hasta il 30 giugno. "
    + "informazioni logistiche sulla conferenza. " * 30
    + "</p></body></html>")


# ---------- euristica -------------------------------------------------------
def test_short_text_with_abstract_line():
    text = "Abstract\n" + _AB_BODY
    assert abstract_only_from({}, text) is True


def test_citation_abstract_meta():
    assert abstract_only_from({"citation_abstract": "Riassunto."},
                              "testo breve") is True


def test_dc_description_meta():
    assert abstract_only_from({"dc.description": "Riassunto."},
                              "testo breve") is True


def test_long_text_is_never_abstract_only():
    long = "parola " * (ABSTRACT_ONLY_MAX_CHARS // 4 + 100)
    assert abstract_only_from({"citation_abstract": "abs"}, long) is False


def test_call_for_abstracts_not_flagged():
    # la parola dentro un titolo NON è una riga Abstract da sola
    text = "Call for abstracts\n" + ("informazioni logistiche. " * 40)
    assert abstract_only_from({}, text) is False


def test_empty_text_not_flagged():
    assert abstract_only_from({}, "") is False
    assert abstract_only_from({}, "   \n  ") is False


# ---------- integrazione extract_html ---------------------------------------
def test_extract_html_pubmed_abstract():
    text, meta = extract_html(PUBMED_ABSTRACT_HTML, "https://x.test/a")
    assert meta["abstract_only"] is True


def test_extract_html_ojs_dc_description():
    _, meta = extract_html(OJS_HTML, "https://x.test/ojs")
    assert meta["abstract_only"] is True


def test_extract_html_full_text():
    text, meta = extract_html(FULL_TEXT_HTML, "https://x.test/b")
    assert meta["abstract_only"] is False


def test_extract_html_container_marker():
    html = ('<html><head><title>S</title></head><body>'
            '<section class="abstract"><p>' + _AB_BODY + '</p></section>'
            "</body></html>")
    _, meta = extract_html(html, "https://x.test/c")
    assert meta["abstract_only"] is True


# ---------- fetcher end-to-end + cache roundtrip -----------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: N802 - silenzia il log di test
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        pages = {"/abs": PUBMED_ABSTRACT_HTML, "/ojs": OJS_HTML,
                 "/full": FULL_TEXT_HTML}
        page = pages.get(self.path)
        if page is None:
            self._send(404, b"nope", "text/plain")
        else:
            self._send(200, page.encode("utf-8"))


@pytest.fixture()
def server_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _settings(tmp_path):
    from app.config import Settings
    return Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "c",
                    cache_enabled=True, fetch_timeout_s=5,
                    per_domain_min_interval_s=0, respect_robots=True,
                    max_concurrency=4)


def _item(url: str) -> SearchResultItem:
    return SearchResultItem(query_id="q1", url=url, title="t", position=0)


async def test_fetcher_flags_abstract_pages(server_url, tmp_path):
    f = Fetcher(_settings(tmp_path), DiskCache(tmp_path / "c", True),
                allow_loopback_for_tests=True)
    try:
        docs = await f.fetch_all([_item(f"{server_url}/abs"),
                                  _item(f"{server_url}/ojs"),
                                  _item(f"{server_url}/full")])
    finally:
        await f.aclose()
    by_path = {d.url.rsplit("/", 1)[-1]: d for d in docs}
    assert getattr(by_path["abs"], "abstract_only") is True
    assert getattr(by_path["ojs"], "abstract_only") is True
    assert getattr(by_path["full"], "abstract_only") is False


async def test_abstract_only_survives_cache_roundtrip(server_url, tmp_path):
    cache = DiskCache(tmp_path / "c", True)
    f = Fetcher(_settings(tmp_path), cache, allow_loopback_for_tests=True)
    try:
        first = (await f.fetch_all([_item(f"{server_url}/abs")]))[0]
        assert first.abstract_only is True
        second = (await f.fetch_all([_item(f"{server_url}/abs")]))[0]
    finally:
        await f.aclose()
    assert getattr(second, "abstract_only") is True  # il flag non si perde in cache


# ---------- storage: migrazione DB vecchio + roundtrip -----------------------
_OLD_SCHEMA = """
CREATE TABLE sources(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, url TEXT, url_final TEXT, domain TEXT,
  title TEXT, http_status INTEGER, content_type TEXT, fetch_status TEXT,
  published_at TEXT, author TEXT, redirects_json TEXT, error TEXT, byte_len INTEGER,
  content_hash TEXT DEFAULT '', identity_key TEXT DEFAULT '',
  source_type TEXT, quality_score INTEGER,
  relevance REAL, accepted INTEGER DEFAULT 0
);
"""


def test_migration_adds_abstract_only_column(tmp_path):
    """DB creato da versione vecchia: la colonna arriva da _migrate, non da
    CREATE TABLE IF NOT EXISTS (che non altera tabelle esistenti)."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_OLD_SCHEMA)
    conn.commit()
    conn.close()

    st = Storage(db)
    try:
        st.add_source("s1", "r1", "u", "u", "d", "t", 200, "text/html",
                      "SUCCESS", None, None, [], None, 100,
                      content_hash="h", identity_key="k", abstract_only=True)
        rows = st.sources_for_run("r1")
    finally:
        st.close()
    assert rows[0]["abstract_only"] == 1


def test_fresh_db_defaults_false(tmp_path):
    st = Storage(tmp_path / "new.db")
    try:
        st.add_source("s1", "r1", "u", "u", "d", "t", 200, "text/html",
                      "SUCCESS", None, None, [], None, 100)
        rows = st.sources_for_run("r1")
    finally:
        st.close()
    assert rows[0]["abstract_only"] == 0
