"""B4: MultiProviderSearchClient — più provider in cascata, risultati mergiati.

DR_SEARCH_PROVIDERS (CSV, es. "duckduckgo,tavily") affianca DR_SEARCH_PROVIDER
(retrocompatibile: vuoto = solo il provider primario). Per ogni query i provider
provano in ordine; i risultati sono dedup-izzati per URL. Un provider che fallisce
non fa crashare la ricerca: si passa al successivo.
"""
from __future__ import annotations

import asyncio
import logging

from app.clients.search.base import SearchApiError, SearchClient
from app.config import SearchProvider, Settings
from app.models import SearchResultItem
from app.security import normalize_for_dedupe

log = logging.getLogger(__name__)


def parse_search_providers(settings: Settings) -> list[SearchProvider]:
    """CSV in settings.search_providers; vuoto/assente = [search_provider].

    Valori sconosciuti: warning e skip (mai crash alla configurazione).
    """
    raw = (settings.search_providers or "").strip()
    if not raw:
        return [settings.search_provider]
    out: list[SearchProvider] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if not name:
            continue
        try:
            p = SearchProvider(name)
        except ValueError:
            log.warning("search_providers: provider sconosciuto %r ignorato", name)
            continue
        if p not in out:
            out.append(p)
    return out or [settings.search_provider]


class MultiProviderSearchClient(SearchClient):
    """Prova i provider in ordine e mergia i risultati dedup per URL."""

    name = "multi"

    def __init__(self, clients: list[SearchClient]):
        if not clients:
            raise SearchApiError("MultiProviderSearchClient: nessun provider")
        self._clients = clients

    async def search(self, query: str,
                     max_results: int) -> list[SearchResultItem]:
        merged: list[SearchResultItem] = []
        seen: set[str] = set()
        failures: list[str] = []
        for client in self._clients:
            try:
                items = await client.search(query, max_results)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # provider down/quota/malformato: il prossimo
                failures.append(f"{getattr(client, 'name', '?')}: {exc}")
                log.warning("provider %s fallito su %r: %s",
                            getattr(client, "name", "?"), query, exc)
                continue
            for it in items:
                key = normalize_for_dedupe(it.url)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(it)
        if not merged and failures:
            raise SearchApiError(
                "tutti i provider falliti (" + "; ".join(failures) + ")")
        return merged[:max_results]

    async def aclose(self) -> None:
        for c in self._clients:
            try:
                await c.aclose()
            except Exception:
                pass


def build_search_clients(settings: Settings) -> list[SearchClient]:
    """Client per TUTTI i provider in settings (primario + extra), senza duplicati."""
    from app.clients.search.base import build_search_client

    clients: list[SearchClient] = []
    for p in parse_search_providers(settings):
        sub = _with_provider(settings, p)
        clients.append(build_search_client(sub))
    return clients


def _with_provider(settings: Settings, provider: SearchProvider) -> Settings:
    """Copia settings con il provider selezionato (le chiavi restano invariate)."""
    data = settings.model_dump()
    data["search_provider"] = provider
    return settings.__class__(**data)
