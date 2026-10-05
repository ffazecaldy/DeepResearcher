"""B4: provider di ricerca multipli (DR_SEARCH_PROVIDERS) — parse, cascade, merge."""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from app.clients.search.base import SearchApiError, SearchClient
from app.clients.search.multi import (MultiProviderSearchClient,
                                      build_search_clients,
                                      parse_search_providers)
from app.config import SearchProvider, Settings
from app.models import SearchResultItem


class _FakeClient(SearchClient):
    name = "fake"

    def __init__(self, results=None, fail=False):
        self._results = results or []
        self._fail = fail
        self.calls = 0

    async def search(self, query, max_results):
        self.calls += 1
        if self._fail:
            raise SearchApiError("boom")
        return list(self._results)[:max_results]


def _item(url: str) -> SearchResultItem:
    return SearchResultItem(query_id="", url=url, title=url, snippet="",
                            position=0)


def _settings(providers_csv: str = "", primary=SearchProvider.DUCKDUCKGO):
    return Settings(_env_file=None, search_provider=primary,
                    search_providers=providers_csv)


# ---------- parse ----------

def test_parse_empty_returns_primary():
    assert parse_search_providers(_settings()) == [SearchProvider.DUCKDUCKGO]


def test_parse_csv_dedupe_and_skip_unknown():
    out = parse_search_providers(_settings("duckduckgo, tavily ,walter,tavily"))
    assert out == [SearchProvider.DUCKDUCKGO, SearchProvider.TAVILY]


def test_parse_only_unknown_falls_back_to_primary():
    out = parse_search_providers(_settings("walter, pippo"))
    assert out == [SearchProvider.DUCKDUCKGO]


# ---------- cascade ----------

def test_multi_merges_and_dedups_by_url():
    c1 = _FakeClient([_item("https://a.example/1"), _item("https://b.example/2")])
    c2 = _FakeClient([_item("https://a.example/1"), _item("https://c.example/3")])
    multi = MultiProviderSearchClient([c1, c2])
    out = asyncio.run(multi.search("q", 10))
    urls = [i.url for i in out]
    assert urls == ["https://a.example/1", "https://b.example/2",
                    "https://c.example/3"]
    assert c1.calls == 1 and c2.calls == 1


def test_multi_skips_failing_provider():
    bad = _FakeClient(fail=True)
    good = _FakeClient([_item("https://ok.example/x")])
    multi = MultiProviderSearchClient([bad, good])
    out = asyncio.run(multi.search("q", 10))
    assert [i.url for i in out] == ["https://ok.example/x"]


def test_multi_all_fail_raises():
    multi = MultiProviderSearchClient([_FakeClient(fail=True)])
    try:
        asyncio.run(multi.search("q", 10))
        raise AssertionError("attesa SearchApiError")
    except SearchApiError:
        pass


def test_multi_empty_clients_rejected():
    try:
        MultiProviderSearchClient([])
        raise AssertionError("attesa SearchApiError")
    except SearchApiError:
        pass


# ---------- builder ----------

def test_build_clients_single_provider_no_multi():
    clients = build_search_clients(_settings())
    assert len(clients) == 1
    assert not isinstance(clients[0], MultiProviderSearchClient)


def test_build_clients_multi_wraps_in_multi():
    # duckduckgo è l'unico provider keyless: CSV con soli duckduckgo-like.
    # duckduckgo ripetuto viene dedup-izzato dal parser, quindi per avere 2
    # client usiamo brave+duckduckgo con una chiave finta per brave.
    s = Settings(_env_file=None, search_provider=SearchProvider.BRAVE,
                 brave_api_key="test-key", search_providers="brave,duckduckgo")
    clients = build_search_clients(s)
    assert len(clients) == 2
