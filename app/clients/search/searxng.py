"""SearXNG client for the JSON search API.

The SearXNG instance must have the JSON format enabled: ``search: formats`` in
its ``settings.yml`` has to include ``json``, otherwise ``/search?format=json``
is rejected by the instance (HTTP 403).
"""
from __future__ import annotations

from app.clients.search.base import _HttpxSearchClient, _parse_items
from app.models import SearchResultItem


class SearxngSearchClient(_HttpxSearchClient):
    """GET ``{base_url}/search?format=json`` (no API key required)."""

    name = "searxng"

    async def search(self, query: str, max_results: int) -> list[SearchResultItem]:
        """Search a self-hosted SearXNG instance; results keep the API order."""
        data = await self._request_json(
            "GET",
            "/search",
            params={"q": query, "format": "json"},
        )
        return _parse_items(
            data,
            client_name=self.name,
            path=("results",),
            snippet_key="content",
            max_results=max_results,
        )


__all__ = ["SearxngSearchClient"]
