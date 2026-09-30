"""Search-client abstraction: shared HTTP/retry plumbing and client factory.

No caching here (the caller caches) and API keys are never logged.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from typing import Any

import httpx

from app.config import SearchProvider, Settings
from app.models import SearchResultItem

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 20.0
RETRY_BACKOFF_S = 2.0
_MAX_ATTEMPTS = 2  # initial attempt + 1 retry
_AUTH_ERROR_MSG = "invalid or missing API key"


class SearchApiError(RuntimeError):
    """A search-API call failed (auth, quota, transport, or malformed payload)."""


class SearchClient(abc.ABC):
    """Common interface for web-search clients.

    Implementations return results in provider order and keep
    ``SearchResultItem.query_id`` empty: the caller sets it.
    """

    name: str

    @abc.abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchResultItem]:
        """Run one web search; return up to ``max_results`` items in provider order."""

    async def aclose(self) -> None:
        """Release underlying HTTP resources (default: no-op)."""
        return None


class _HttpxSearchClient(SearchClient):
    """Shared plumbing: one ``httpx.AsyncClient`` plus JSON requests with retry."""

    name = "search"

    def __init__(self, base_url: str) -> None:
        self._base_url = _require_base_url(self.name, base_url)
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request against ``self._base_url`` and return the JSON object."""
        return await _send_json(
            self._http,
            method=method,
            url=f"{self._base_url}{path}",
            client_name=self.name,
            headers=headers,
            params=params,
            json_body=json_body,
        )


async def _send_json(
    http: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    client_name: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one JSON request; retry once on 429/5xx/timeout with a 2s backoff.

    401/403 map to ``SearchApiError`` with a message that never echoes the key.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = await http.request(
                method, url, headers=headers, params=params, json=json_body
            )
        except httpx.TimeoutException:
            if attempt < _MAX_ATTEMPTS:
                log.warning("%s: request timed out, retrying", client_name)
                await asyncio.sleep(RETRY_BACKOFF_S)
                continue
            raise SearchApiError(f"{client_name}: request timed out") from None
        if resp.status_code in (401, 403):
            raise SearchApiError(f"{client_name}: {_AUTH_ERROR_MSG}")
        if resp.status_code < 400:
            try:
                data = resp.json()
            except ValueError as exc:
                raise SearchApiError(
                    f"{client_name}: response is not valid JSON"
                ) from exc
            if not isinstance(data, dict):
                raise SearchApiError(f"{client_name}: unexpected response shape")
            return data
        if attempt < _MAX_ATTEMPTS and (
            resp.status_code == 429 or resp.status_code >= 500
        ):
            log.warning("%s: HTTP %d, retrying", client_name, resp.status_code)
            await asyncio.sleep(RETRY_BACKOFF_S)
            continue
        raise SearchApiError(f"{client_name}: HTTP {resp.status_code}")
    raise SearchApiError(f"{client_name}: request failed")  # pragma: no cover


def _require_base_url(client_name: str, base_url: str) -> str:
    """Return the stripped base URL or raise if not configured."""
    url = base_url.strip().rstrip("/")
    if not url:
        raise SearchApiError(f"{client_name}: base URL is not configured")
    return url


def _require_api_key(client_name: str, api_key: str) -> None:
    """Fail fast on an empty API key (the message never echoes the key)."""
    if not api_key.strip():
        raise SearchApiError(f"{client_name}: {_AUTH_ERROR_MSG}")


def _parse_items(
    data: dict[str, Any],
    *,
    client_name: str,
    path: tuple[str, ...],
    snippet_key: str,
    max_results: int,
) -> list[SearchResultItem]:
    """Walk ``data`` along ``path`` to the result list; map entries in order.

    ``query_id`` stays empty (the caller sets it); ``position`` is 1-based.
    """
    node: Any = data
    for key in path:
        if not isinstance(node, dict):
            node = None
            break
        node = node.get(key)
    if not isinstance(node, list):
        raise SearchApiError(f"{client_name}: unexpected response shape")
    items: list[SearchResultItem] = []
    for row in node:
        if not isinstance(row, dict):
            continue
        items.append(
            SearchResultItem(
                query_id="",
                url=str(row.get("url") or ""),
                title=str(row.get("title") or ""),
                snippet=str(row.get(snippet_key) or ""),
                position=len(items) + 1,
            )
        )
        if len(items) >= max_results:
            break
    return items


def build_search_client(settings: Settings) -> SearchClient:
    """Build the search client selected by ``settings.search_provider``.

    Concrete clients are imported lazily so they can import this module freely.
    """
    from app.clients.search.brave import BraveSearchClient
    from app.clients.search.searxng import SearxngSearchClient
    from app.clients.search.tavily import TavilySearchClient

    provider = settings.search_provider
    if provider is SearchProvider.TAVILY:
        return TavilySearchClient(
            api_key=settings.tavily_api_key, base_url=settings.tavily_base_url
        )
    if provider is SearchProvider.BRAVE:
        return BraveSearchClient(
            api_key=settings.brave_api_key, base_url=settings.brave_base_url
        )
    if provider is SearchProvider.SEARXNG:
        return SearxngSearchClient(base_url=settings.searxng_base_url)
    if provider is SearchProvider.DUCKDUCKGO:
        from app.clients.search.duckduckgo import DuckDuckGoSearchClient
        return DuckDuckGoSearchClient()
    raise SearchApiError(f"unsupported search provider: {provider!r}")
