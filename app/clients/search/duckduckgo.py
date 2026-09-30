"""DuckDuckGo HTML search client: keyless provider (best-effort, no API)."""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from app.clients.search.base import SearchApiError
from app.models import SearchResultItem

log = logging.getLogger(__name__)

_ENDPOINT = "https://html.duckduckgo.com/html/"


def _clean_url(href: str) -> str:
    """DDG wraps results in /l/?uddg=<encoded>; unwrap to the real target."""
    if "duckduckgo.com/l/" in href:
        q = parse_qs(urlsplit(href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    if href.startswith("//"):
        return f"https:{href}"
    return href


def _strip_tags(s: str) -> str:
    import re as _re
    return _re.sub(r"<[^>]+>", "", s)


def _parse_results(html: str, max_results: int) -> list[SearchResultItem]:
    """Parse the html.duckduckgo.com SERP from result__a anchors.

    NOTE: the wrapper div carries multiple classes
    (class="links_main links_deep result__body"), so splitting on the bare
    class name would miss it: anchor on result__a instead.
    """
    import html as _html

    out: list[SearchResultItem] = []
    for chunk in html.split('class="result__a"')[1:]:
        if len(out) >= max_results:
            break
        href_start = chunk.find('href="')
        if href_start == -1:
            continue
        href = chunk[href_start + 6:]
        href = href[:href.find('"')]
        gt = chunk.find(">", href_start)
        if gt == -1:
            continue
        title = _strip_tags(chunk[gt + 1:chunk.find("<", gt)]).strip()
        url = _clean_url(href)
        if not url.startswith("http") or not title:
            continue
        snip = ""
        s = chunk.find('class="result__snippet"')
        if s != -1:
            seg = chunk[s:]
            seg = seg[seg.find(">") + 1:]
            end = min(x for x in (seg.find("</a>"), seg.find("</div>"),
                                  len(seg)) if x != -1)
            snip = _strip_tags(seg[:end]).strip()
        out.append(SearchResultItem(
            query_id="", url=url, title=_html.unescape(title),
            snippet=_html.unescape(snip), position=len(out) + 1))
    if not out:
        raise SearchApiError("duckduckgo: risposta non riconosciuta (anti-bot?)")
    return out


class DuckDuckGoSearchClient:
    """Scrapes the DDG HTML endpoint; keyless but more fragile than APIs."""

    name = "duckduckgo"

    def __init__(self, timeout_s: float = 20.0):
        # plain browser UA: DDG rejects UAs advertising automation
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0 Safari/537.36",
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )

    async def search(self, query: str, max_results: int) -> list[SearchResultItem]:
        try:
            resp = await self._http.post(_ENDPOINT, data={"q": query})
        except httpx.HTTPError as exc:
            raise SearchApiError(f"duckduckgo: {exc}") from exc
        if resp.status_code != 200:
            raise SearchApiError(f"duckduckgo: http {resp.status_code}")
        return _parse_results(resp.text, max_results)

    async def aclose(self) -> None:
        await self._http.aclose()
