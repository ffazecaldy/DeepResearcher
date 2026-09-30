"""Brave Search API client (https://api.search.brave.com/res/v1)."""
from __future__ import annotations

from app.clients.search.base import (
    SearchApiError,
    _HttpxSearchClient,
    _parse_items,
    _require_api_key,
)
from app.models import SearchResultItem


class BraveSearchClient(_HttpxSearchClient):
    """GET ``{base_url}/web/search`` with ``X-Subscription-Token: <key>``."""

    name = "brave"

    def __init__(self, *, api_key: str, base_url: str) -> None:
        _require_api_key(self.name, api_key)
        self._api_key = api_key
        super().__init__(base_url)

    async def search(self, query: str, max_results: int) -> list[SearchResultItem]:
        """Web search; results keep the API order."""
        data = await self._request_json(
            "GET",
            "/web/search",
            params={"q": query, "count": str(max_results)},
            headers={
                "X-Subscription-Token": self._api_key,
                "Accept": "application/json",
            },
        )
        return _parse_items(
            data,
            client_name=self.name,
            path=("web", "results"),
            snippet_key="description",
            max_results=max_results,
        )


__all__ = ["BraveSearchClient", "SearchApiError"]
