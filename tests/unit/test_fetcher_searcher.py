"""Fetcher + Searcher tests: local HTTP server (no external network)."""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agent.fetcher import Fetcher
from app.agent.searcher import Searcher
from app.config import Settings
from app.limits import DiskCache
from app.models import GeneratedQuery, SearchResultItem

PAGE = ("<html><head><title>Telefono</title></head><body>"
        + "<p>Bell brevettò il telefono nel 1876 dopo anni di esperimenti. </p>" * 12
        + "</body></html>")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/robots.txt":
            self._send(200, b"User-agent: *\nDisallow: /blocked\n",
                       "text/plain")
        elif self.path == "/a":
            self._send(200, PAGE.encode("utf-8"))
        elif self.path == "/redirect":
            self._send(302, b"", headers={"Location": "/a"})
        elif self.path == "/empty":
            self._send(200, b"<html><body></body></html>")
        else:
            self._send(404, b"nope", "text/plain")


@pytest.fixture()
def server_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "c",
                    cache_enabled=False, fetch_timeout_s=5,
                    per_domain_min_interval_s=0, respect_robots=True,
                    max_concurrency=4)


def _fetcher(tmp_path) -> Fetcher:
    return Fetcher(_settings(tmp_path), DiskCache(tmp_path / "c", False),
                   allow_loopback_for_tests=True)


def _item(url: str, qid: str = "q1") -> SearchResultItem:
    return SearchResultItem(query_id=qid, url=url, title="t", position=0)


async def test_fetch_success_and_extract(server_url, tmp_path):
    f = _fetcher(tmp_path)
    try:
        docs = await f.fetch_all([_item(f"{server_url}/a")])
    finally:
        await f.aclose()
    d = docs[0]
    assert d.fetch_status.value == "SUCCESS"
    assert d.title == "Telefono"
    assert "1876" in d.text
    assert d.domain.startswith("127.0.0.1")


async def test_fetch_redirect_followed(server_url, tmp_path):
    f = _fetcher(tmp_path)
    try:
        docs = await f.fetch_all([_item(f"{server_url}/redirect")])
    finally:
        await f.aclose()
    d = docs[0]
    assert d.fetch_status.value == "SUCCESS"
    assert d.redirects and d.final_url.endswith("/a")


async def test_fetch_404_and_empty(server_url, tmp_path):
    from urllib.parse import urlsplit
    f = _fetcher(tmp_path)
    try:
        docs = await f.fetch_all([_item(f"{server_url}/missing"),
                                  _item(f"{server_url}/empty")])
    finally:
        await f.aclose()
    statuses = {urlsplit(d.url).path: d.fetch_status.value for d in docs}
    assert statuses["/missing"] == "FETCH_FAILED"
    assert statuses["/empty"] == "EXTRACTION_FAILED"


async def test_fetch_robots_disallow(server_url, tmp_path):
    f = _fetcher(tmp_path)
    try:
        docs = await f.fetch_all([_item(f"{server_url}/blocked")])
    finally:
        await f.aclose()
    assert docs[0].fetch_status.value == "FETCH_FAILED"
    assert "robots" in (docs[0].error or "")


async def test_fetch_loopback_blocked_by_default(server_url, tmp_path):
    f = Fetcher(_settings(tmp_path), DiskCache(tmp_path / "c", False))
    try:
        docs = await f.fetch_all([_item(f"{server_url}/a")])
    finally:
        await f.aclose()
    assert docs[0].fetch_status.value == "FETCH_FAILED"
    assert "unsafe url" in (docs[0].error or "")


class _FakeSearchClient:
    name = "fake"

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int):
        self.queries.append(query)
        base = "https://Example.net/x/?utm_source=zz&id=1"
        if query == "prima":
            return [SearchResultItem(query_id="", url=base, title="A", position=0),
                    SearchResultItem(query_id="", url="https://example.net/y",
                                     title="B", position=1)]
        return [SearchResultItem(query_id="", url=base + "&fbclid=2",
                                 title="A dup", position=0)]

    async def aclose(self) -> None:
        pass


async def test_searcher_dedup_and_tracking(tmp_path):
    client = _FakeSearchClient()
    s = Searcher(client, DiskCache(tmp_path / "c", False), _settings(tmp_path))
    queries = [GeneratedQuery(query_id="q1", subquestion_id="sq1", text="prima"),
               GeneratedQuery(query_id="q2", subquestion_id="sq1", text="seconda")]

    out = await s.run_queries(queries, max_pages_per_query=5)

    urls = [i.url for i in out]
    # same normalized URL twice -> once; tracking params stripped
    assert len(out) == 2
    assert sum(1 for u in urls if "example.net/x" in u.lower()) == 1
    kept = next(i for i in out if "example.net/x" in i.url.lower())
    assert "utm_source" not in kept.url and "fbclid" not in kept.url
    assert kept.query_id == "q1"  # first finder keeps provenance
    # per-query results attributed
    assert all(i.query_id in {"q1", "q2"} for i in out)


async def test_searcher_query_failure_isolated(tmp_path):
    class Flaky(_FakeSearchClient):
        async def search(self, query, max_results):
            if query == "bad":
                raise RuntimeError("api down")
            return await super().search(query, max_results)

    s = Searcher(Flaky(), DiskCache(tmp_path / "c", False), _settings(tmp_path))
    queries = [GeneratedQuery(query_id="q1", subquestion_id="sq1", text="prima"),
               GeneratedQuery(query_id="q2", subquestion_id="sq1", text="bad")]
    out = await s.run_queries(queries, 5)
    assert out and all(i.query_id == "q1" for i in out)


@pytest.mark.live
async def test_live_example_com(tmp_path):
    f = _fetcher(tmp_path)
    try:
        docs = await f.fetch_all([_item("https://example.com")])
    finally:
        await f.aclose()
    assert docs[0].fetch_status.value in ("SUCCESS", "PARTIAL_CONTENT")
    assert len(docs[0].text) > 50  # real extraction from the live page
