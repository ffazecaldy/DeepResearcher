"""Dedup per contenuto: normalizzazione content_hash e lookup su storage."""
from __future__ import annotations

import hashlib
import os
import sqlite3

from app.agent.fetcher import content_fingerprint
from app.models import FetchedDocument, FetchStatus
from app.storage import Storage


def test_same_text_different_url_same_hash():
    t = "  Il tempeRamento   del CLIMA\n\ne gli  estivi\teccellenti.  "
    d1 = FetchedDocument(url="https://a.example.com/x", text=t,
                         fetch_status=FetchStatus.SUCCESS)
    d2 = FetchedDocument(url="https://b.example.org/copy",
                         text="Il temperamento del clima e gli estivi eccellenti.",
                         fetch_status=FetchStatus.PARTIAL_CONTENT)
    assert d1.content_hash == ""  # default finche' il fetcher non lo calcola
    assert content_fingerprint(d1.text) == content_fingerprint(d2.text)
    assert content_fingerprint(d1.text) != ""


def test_hash_is_sha256_of_normalized_text():
    assert content_fingerprint("Ciao Mondo") == \
        hashlib.sha256(b"ciao mondo").hexdigest()


def test_empty_text_yields_empty_hash():
    assert content_fingerprint("") == ""
    assert content_fingerprint("   \n\t  ") == ""


def test_hash_capped_at_200k_chars():
    long_text = "a" * 250_000
    assert content_fingerprint(long_text) == content_fingerprint("a" * 200_000)
    assert content_fingerprint(long_text) != content_fingerprint("a" * 199_999 + "b")


def _storage(db_path) -> Storage:
    return Storage(db_path)


def test_storage_content_hash_roundtrip_and_scope(tmp_path):
    s = _storage(os.path.join(tmp_path, "r.db"))
    s.create_run("run1", "q?", "it", "standard")
    s.create_run("run2", "q?", "it", "standard")
    h = content_fingerprint("testo condiviso")
    s.add_source("src1", "run1", "https://a.example.com", "", "a.example.com",
                 "T", 200, "text/html", "SUCCESS", None, None, [], None, 500, h)
    # add_source senza content_hash: default "" (back-compat col chiamante attuale)
    s.add_source("src2", "run1", "https://b.example.com", "", "b.example.com",
                 "T", 200, "text/html", "SUCCESS", None, None, [], None, 500)
    assert s.content_hash_seen("run1", h) is True
    assert s.content_hash_seen("run2", h) is False  # scope: solo lo stesso run
    assert s.content_hash_seen("run1", "missing") is False
    assert s.content_hash_seen("run1", "") is False  # hash vuoto mai deduplicato
    s.close()


def test_migration_adds_content_hash_to_existing_db(tmp_path):
    """DB creato con lo schema vecchio + dati: la colonna viene aggiunta in
    modo idempotente e i dati sopravvivono."""
    db_path = os.path.join(tmp_path, "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
CREATE TABLE sources(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, url TEXT, url_final TEXT, domain TEXT,
  title TEXT, http_status INTEGER, content_type TEXT, fetch_status TEXT,
  published_at TEXT, author TEXT, redirects_json TEXT, error TEXT, byte_len INTEGER,
  source_type TEXT, quality_score INTEGER, relevance REAL, accepted INTEGER DEFAULT 0);
INSERT INTO sources(id, run_id, url, byte_len)
  VALUES('old1', 'run0', 'https://old.example.com', 42);
""")
    conn.commit()
    conn.close()

    s = Storage(db_path)   # prima apertura: migra
    s.close()
    s = Storage(db_path)   # seconda apertura: idempotente
    try:
        cols = {r["name"] for r in s._query("PRAGMA table_info(sources)")}
        assert "content_hash" in cols
        row = s._query("SELECT url, byte_len, content_hash FROM sources WHERE id='old1'")[0]
        assert row["url"] == "https://old.example.com"
        assert row["byte_len"] == 42
        assert row["content_hash"] == ""  # dati pre-esistenti: hash vuoto
        h = content_fingerprint("nuovo testo")
        s.add_source("new1", "run0", "https://n.example.com", "", "n.example.com",
                     "T", 200, "text/html", "SUCCESS", None, None, [], None, 7, h)
        assert s.content_hash_seen("run0", h) is True
    finally:
        s.close()
