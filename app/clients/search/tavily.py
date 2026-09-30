"""Tavily Search API client (https://api.tavily.com)."""
from __future__ import annotations

from app.clients.search.base import (
    SearchApiError,
    _HttpxSearchClient,
    _parse_items,
    _require_api_key,
)
from app.models import SearchResultItem


class TavilySearchClient(_HttpxSearchClient):
    """POST ``{base_url}/search`` with ``Authorization: Bearer <key>``."""

    name = "tavily"

    def __init__(self, *, api_key: str, base_url: str) -> None:
        _require_api_key(self.name, api_key)
        self._api_key = api_key
        super().__init__(base_url)

    async def search(self, query: str, max_results: int) -> list[SearchResultItem]:
        """Basic-depth search; results keep the API order."""
        data = await self._request_json(
            "POST",
            "/search",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json_body={
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
        return _parse_items(
            data,
            client_name=self.name,
            path=("results",),
            snippet_key="content",
            max_results=max_results,
        )


__all__ = ["SearchApiError", "TavilySearchClient"]
