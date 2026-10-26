"""DuckDuckGo HTML search client: keyless provider (best-effort, no API)."""
from __future__ import annotations

import base64
import logging
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

import httpx

from app.clients.search.base import SearchApiError
from app.models import SearchResultItem

log = logging.getLogger(__name__)

_ENDPOINT = "https://html.duckduckgo.com/html/"

# Parametri che MARCANO un risultato come sponsorizzato/affiliato: presenti ->
# il "documento" e' un annuncio, non una fonte. Si prova a estrarre la
# destinazione reale (uddg/u3/url/...); se assente o invalida, si scarta.
_AD_PARAM_MARKERS = {
    "ad_domain", "ad_provider", "ad_provider_id", "ad_image_url", "ad_click",
    "ad_type", "aff", "affiliate", "affid", "affiliate_id", "partner_id",
}
# Parametri di TRACKING puro: non cambiano la destinazione, vengono rimossi
# dall'URL finale cosi' il provenance non registra identificativi campagne.
_TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "dclid", "twclid",
                    "mc_cid", "mc_eid"}
_TRACKING_PREFIXES = ("utm_",)
# Host/path intermedi riconosciuti come PUBBLICITA' (redirect di ad network):
# duckduckgo.com/y.js = risultati sponsorizzati DDG; /aclk (Google), /aclick
# (Bing); i restanti sono ad network/affiliazione classici.
_AD_INTERMEDIATE_PATHS = {"/y.js", "/aclk", "/aclick"}
_AD_NETWORK_HOSTS = {
    "clickserve.dartsearch.net", "www.googleadservices.com",
    "www.anrdoezrs.net", "click.linksynergy.com", "www.awin1.com",
    "track.adform.net", "www.tkqlhce.com", "www.jdoqocy.com",
}
# parametri che possono contenere l'URL di destinazione reale
_TARGET_PARAM_KEYS = ("uddg", "u", "url", "u3", "target", "destination")


def _is_valid_http(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _decode_u3(raw: str) -> str:
    """Decodifica il parametro u3 dei link sponsorizzati DDG (base64url)."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
    except Exception:
        return raw  # forse non era base64: la validazione a valle decidera'


def _is_ad_intermediate(url: str) -> bool:
    """True se l'URL e' un INTERMEDIO pubblicitario: host/path di ad network
    (duckduckgo.com/y.js, googleadservices, /aclk, /aclick, ...)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host in _AD_NETWORK_HOSTS:
        return True
    if host in ("duckduckgo.com", "www.duckduckgo.com") and \
            path in _AD_INTERMEDIATE_PATHS:
        return True
    return path in _AD_INTERMEDIATE_PATHS


def _is_ad_redirect(url: str) -> bool:
    """True se l'URL e' SPONSORIZZATO: intermedio pubblicitario OPPURE parametri
    ad/affiliazione presenti (es. ad_domain sui risultati sponsorizzati DDG)."""
    if _is_ad_intermediate(url):
        return True
    try:
        q = parse_qs(urlsplit(url).query)
    except ValueError:
        return False
    return bool(_AD_PARAM_MARKERS & set(q))


def _extract_ad_target(url: str) -> str | None:
    """URL reale di destinazione di un annuncio, se presente e valido."""
    q = parse_qs(urlsplit(url).query)
    for key in _TARGET_PARAM_KEYS:
        vals = q.get(key)
        if not vals:
            continue
        raw = _decode_u3(vals[0]) if key == "u3" else unquote(vals[0])
        if key == "q" and not raw.lower().startswith(("http://", "https://")):
            continue  # q= e' spesso un termine di ricerca, non una destinazione
        if _is_valid_http(raw) and not _is_ad_redirect(raw):
            return raw
    return None


def _strip_ad_tracking_params(url: str) -> str:
    """Rimuove parametri ad/affiliazione/tracking residui da un URL organic."""
    try:
        parts = urlsplit(url)
        q = parse_qs(parts.query, keep_blank_values=True)
    except ValueError:
        return url
    filtered = []
    for k, vals in q.items():
        if k in _AD_PARAM_MARKERS or k in _TRACKING_PARAMS or \
                k.startswith(_TRACKING_PREFIXES):
            continue
        filtered.append((k, vals[0]))
    # ricostruisce SEMPRE: se tutti i parametri erano ad/tracking la query
    # va VUOTA (l'early-return here reintrodurrebbe i parametri pubblicitari)
    return urlunsplit(parts._replace(query=urlencode(filtered)))


def _resolve_result_url(href: str) -> str | None:
    """URL da registrare per un risultato SERP.

    Organico -> URL pulito (wrapper /l/?uddg= rimosso, tracking spogliato).
    Intermedio pubblicitario (duckduckgo.com/y.js, ad network) -> destinazione
    reale se estraibile e valida, ALTRIMENTI None: l'URL pubblicitario NON e'
    mai registrato come documento. Un host normale con SOLO parametri
    di affiliazione/tracking resta un documento: i parametri vengono spogliati.
    """
    url = _clean_url(href)
    if not _is_valid_http(url):
        return None
    if _is_ad_intermediate(url):
        target = _extract_ad_target(url)
        if target:
            log.info("duckduckgo: risultato sponsorizzato -> destinazione "
                     "reale %s", target)
            return _strip_ad_tracking_params(target)
        log.info("duckduckgo: scartato annuncio senza destinazione valida: %s",
                 url)
        return None
    return _strip_ad_tracking_params(url)


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
        url = _resolve_result_url(href)
        if url is None:
            continue  # annuncio senza destinazione valida: NON e' un documento
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
        # B1-fix5: DDG html risponde 202 con body vuoto quando rate-limita
        # (soft-block keyless). 2 retry con backoff: spesso al 2o tenta passa.
        last = ""
        for attempt in (1, 2, 3):
            try:
                resp = await self._http.post(_ENDPOINT, data={"q": query})
            except httpx.HTTPError as exc:
                raise SearchApiError(f"duckduckgo: {exc}") from exc
            if resp.status_code == 200:
                return _parse_results(resp.text, max_results)
            last = f"http {resp.status_code}"
            if resp.status_code in (202, 429, 403):
                import asyncio as _a
                await _a.sleep(1.5 * attempt)  # backoff crescente
                continue
            raise SearchApiError(f"duckduckgo: {last}")
        raise SearchApiError(f"duckduckgo: {last} (rate-limit dopo 3 tentativi)")

    async def aclose(self) -> None:
        await self._http.aclose()
