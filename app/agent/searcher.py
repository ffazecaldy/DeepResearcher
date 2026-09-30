"""Searcher: runs plan queries through a SearchClient with cache + dedup."""
from __future__ import annotations

import asyncio
import logging

from app.clients.search.base import SearchClient
from app.limits import DiskCache
from app.models import GeneratedQuery, SearchResultItem
from app.security import normalize_for_dedupe, strip_tracking, validate_public_http_url

log = logging.getLogger(__name__)


class Searcher:
    """Query fan-out: cached, parallel, SSRF-filtered, deduplicated."""

    def __init__(self, client: SearchClient, cache: DiskCache, settings):
        self._client = client
        self._cache = cache
        self._settings = settings

    def _cache_key(self, query: str, max_results: int) -> str:
        from app.limits import DiskCache
        return DiskCache._key(self._client.name, query, str(max_results))

    async def _run_one(self, query: GeneratedQuery,
                       max_results: int) -> list[SearchResultItem]:
        key = self._cache_key(query.text, max_results)
        cached = self._cache.get_json("search", key)
        if isinstance(cached, list):
            items = [SearchResultItem(**d) for d in cached]
        else:
            raw = await self._client.search(query.text, max_results)
            items = []
            for i, r in enumerate(raw):
                items.append(SearchResultItem(
                    query_id=query.query_id, url=r.url, title=r.title,
                    snippet=r.snippet, position=i,
                ))
            self._cache.set_json("search", key,
                                 [i.model_dump() for i in items])
        for it in items:
            it.query_id = query.query_id
        return items

    async def run_queries(self, queries: list[GeneratedQuery],
                          max_pages_per_query: int) -> list[SearchResultItem]:
        """Run all queries in parallel; per-query failures never crash the run."""
        results = await asyncio.gather(
            *(self._run_one(q, max_pages_per_query) for q in queries),
            return_exceptions=True,
        )
        flat: list[SearchResultItem] = []
        for q, res in zip(queries, results):
            if isinstance(res, BaseException):
                log.warning("search failed for query %r: %s", q.text, res)
                continue
            flat.extend(res)

        out: list[SearchResultItem] = []
        seen: set[str] = set()
        for item in flat:
            try:
                url = strip_tracking(item.url)
                validate_public_http_url(url,
                                         blocked_domains=self._settings.blocked_domains())
            except Exception as exc:  # unsafe/unparsable URL: drop
                log.debug("searcher: dropped URL %r (%s)", item.url, exc)
                continue
            item.url = url
            key = normalize_for_dedupe(url)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out
