"""Fetcher: concurrent page download with SSRF guard, robots.txt, rate limit, cache.

CONVENZIONE ATTRIBUTI DINAMICI (models.py e' condiviso con altri owner, il
modello FetchedDocument non si estende): il fetcher calcola e attacca al doc
tre metadati FUORI schema via ``object.__setattr__`` (pydantic v2 rifiuta
l'assegnazione normale di campi non dichiarati):

- ``doc.identity_key``: chiave primaria d'identita' bibliografica con
  priorita' ``doi:... > pmid:... > pmc:... > canon:... > url:...`` (DOI
  normalizzato lowercase; PMID/PMCID numerici/PMC-id; canonical e URL
  normalizzati con normalize_for_dedupe). Sempre non-vuota per un fetch ok.
- ``doc.identity_aliases``: lista di TUTTE le chiavi d'identita' presenti
  (la primaria in testa). L'orchestrator deduplica se la chiave primaria O
  una qualsiasi alias e' gia' vista nel run: cosi' la copia PMC di uno studio
  PubMed collide anche quando solo una delle due pagine porta il DOI.
- ``doc.published_at_validated``: True solo se ``published_at`` proviene
  dalla validazione di extract.py (JSON-LD/meta citation_* o candidata
  plausibile non-bibliografica). ``published_at`` e' data di pubblicazione;
  la data di RECUPERO resta ``fetched_at``/``created_at`` del doc.

Lettura lato consumatore: SEMPRE ``getattr(doc, "<attr>", default)``.
Gli attributi non entrano in ``model_dump()``: il roundtrip della cache disco
li salva esplicitamente (``_to_cache``/``_from_cache``) e l'orchestrator
li persiste in ``sources.identity_key`` via ``storage.add_source``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from app.extraction.extract import extract_html
from app.extraction.pdf import extract_pdf
from app.limits import DiskCache, LimitReached, RuntimeBudget
from app.models import FetchStatus, FetchedDocument, SearchResultItem
from app.security import validate_public_http_url

log = logging.getLogger(__name__)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

_HASH_MAX_CHARS = 200_000

# attributi dinamici fuori-schema (vedi docstring modulo)
_DYNAMIC_META_KEYS = ("identity_key", "identity_aliases",
                      "published_at_validated")
_DYNAMIC_DEFAULTS: dict = {"identity_key": "", "identity_aliases": [],
                           "published_at_validated": False}


def set_dynamic_meta(doc: FetchedDocument, key: str, value) -> None:
    """Attacca un metadato fuori-schema al documento (vedi docstring modulo)."""
    object.__setattr__(doc, key, value)


def ensure_dynamic_meta(doc: FetchedDocument) -> None:
    """Garantisce che i tre attributi dinamici ESISTANO sempre (default vuoti),
    cosi' il consumatore puo' fare getattr senza sorprese."""
    for key, default in _DYNAMIC_DEFAULTS.items():
        if not hasattr(doc, key):
            object.__setattr__(doc, key, default)


def _norm_url_key(url: str) -> str:
    from app.security import normalize_for_dedupe
    if not url:
        return ""
    try:
        return normalize_for_dedupe(url)
    except Exception:  # noqa: BLE001 - URL patologico: chiave non normalizzata
        return url.strip()


def identity_keys(meta: dict, doc_url: str,
                  final_url: str = "") -> tuple[str, list[str]]:
    """(identity_key, identity_aliases) dai campi identita' di extract.py.

    Priorita' DOI > PMID > PMCID > canonical URL > url normalizzato. Le alias
    contengono TUTTE le chiavi presenti: la dedup per intersezione colla
    PubMed<->PMC anche quando il DOI e' su una sola delle due pagine.
    """
    parts: list[str] = []
    doi = (meta.get("doi") or "").strip().lower()
    pmid = (meta.get("pmid") or "").strip()
    pmcid = (meta.get("pmcid") or "").strip().upper()
    canon = _norm_url_key(meta.get("canonical_url") or "")
    base = _norm_url_key(final_url or doc_url or "")
    if doi:
        parts.append(f"doi:{doi}")
    if pmid:
        parts.append(f"pmid:{pmid}")
    if pmcid:
        parts.append(f"pmc:{pmcid}")
    if canon:
        parts.append(f"canon:{canon}")
    if base:
        parts.append(f"url:{base}")
    if not parts:
        return "", []
    return parts[0], parts


def content_fingerprint(text: str) -> str:
    """sha256 del testo normalizzato per la dedup per CONTENUTO.

    Normalizzazione: lowercase, ogni sequenza di whitespace collassata a un
    singolo spazio, trim, primi 200k caratteri. Due pagine con lo stesso
    testo su URL diversi producono lo stesso hash (non fonti indipendenti).
    Restituisce "" per testo vuoto/solo spazi.
    """
    norm = re.sub(r"\s+", " ", text.lower()).strip()[:_HASH_MAX_CHARS]
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


class Fetcher:
    """Downloads and extracts pages; one bad source never fails the run."""

    def __init__(self, settings, cache: DiskCache, *,
                 allow_loopback_for_tests: bool = False):
        self._settings = settings
        self._cache = cache
        self._allow_private = allow_loopback_for_tests
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.fetch_timeout_s),
            follow_redirects=False,
            headers={"User-Agent": settings.fetch_user_agent},
        )
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._domain_last: dict[str, float] = {}
        self._domain_lock = asyncio.Lock()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- helpers ----------
    def _validate(self, url: str) -> str:
        return validate_public_http_url(
            url, blocked_domains=self._settings.blocked_domains(),
            allow_private=self._allow_private,
        )

    async def _rate_limit(self, domain: str) -> None:
        interval = self._settings.per_domain_min_interval_s
        async with self._domain_lock:
            wait = interval - (time.monotonic() - self._domain_last.get(domain, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._domain_last[domain] = time.monotonic()

    async def _robots_allows(self, url: str, netloc: str) -> bool:
        if not self._settings.respect_robots:
            return True
        if netloc not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                sp = urlsplit(url)
                resp = await self._client.get(f"{sp.scheme}://{netloc}/robots.txt")
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                else:
                    parser = None  # no/failed robots.txt -> allow
            except Exception:
                parser = None
            self._robots[netloc] = parser
        parser = self._robots[netloc]
        if parser is None:
            return True
        return parser.can_fetch(self._settings.fetch_user_agent, url)

    # ---------- fetch ----------
    async def fetch_all(self, items: list[SearchResultItem], budget: RuntimeBudget | None = None,
                        on_event=None) -> list[FetchedDocument]:
        """Fetch all items; ``on_event(kind, url)`` reports queued/fetching live."""
        tasks = [asyncio.create_task(self._one(item, budget, on_event))
                 for item in items]
        return list(await asyncio.gather(*tasks))

    def _cache_key(self, url: str) -> str:
        from app.limits import DiskCache
        return DiskCache._key(url)

    def _from_cache(self, url: str) -> FetchedDocument | None:
        raw = self._cache.get_json("fetch", self._cache_key(url))
        if not isinstance(raw, dict):
            return None
        doc = FetchedDocument(**raw)
        # identity belongs to the run, content is cacheable: fresh id per load
        doc.source_id = FetchedDocument(url="").source_id
        # roundtrip dei metadati dinamici (fuori model_dump, vedi docstring)
        for key in _DYNAMIC_META_KEYS:
            if key in raw:
                object.__setattr__(doc, key, raw[key])
        ensure_dynamic_meta(doc)
        return doc

    def _to_cache(self, doc: FetchedDocument) -> None:
        if doc.fetch_status in (FetchStatus.FETCH_FAILED,):
            return  # transient failures are not cached
        payload = doc.model_copy(update={"text": doc.text[:400_000]})
        dump = payload.model_dump()
        for key in _DYNAMIC_META_KEYS:
            dump[key] = getattr(doc, key, _DYNAMIC_DEFAULTS[key])
        self._cache.set_json("fetch", self._cache_key(doc.url), dump)

    async def _one(self, item: SearchResultItem, budget: RuntimeBudget | None,
                   on_event=None) -> FetchedDocument:
        try:
            url = self._validate(item.url)
        except Exception as exc:
            doc = FetchedDocument(url=item.url, fetch_status=FetchStatus.FETCH_FAILED,
                                  error=f"unsafe url: {exc}")
            ensure_dynamic_meta(doc)
            return doc
        if on_event:
            try:
                on_event("source_queued", url)
            except Exception:
                pass
        cached = self._from_cache(url)
        if cached is not None:
            if on_event:
                try:
                    on_event("source_cache_hit", url)
                except Exception:
                    pass
            return cached
        if on_event:
            try:
                on_event("source_fetching", url)
            except Exception:
                pass
        if budget is not None:
            try:
                budget.consume_page()
            except LimitReached as exc:
                doc = FetchedDocument(url=url, fetch_status=FetchStatus.FETCH_FAILED,
                                      error=f"not fetched, {exc}")
                ensure_dynamic_meta(doc)
                return doc
        async with self._sem:
            doc = await self._fetch_and_extract(url)
            ensure_dynamic_meta(doc)
            return doc

    async def _fetch_and_extract(self, url: str) -> FetchedDocument:
        doc = FetchedDocument(url=url)
        try:
            current = url
            for _hop in range(self._settings.fetch_max_redirects + 1):
                sp = urlsplit(current)
                netloc = sp.netloc
                domain = sp.hostname or ""
                if not await self._robots_allows(current, netloc):
                    doc.fetch_status = FetchStatus.FETCH_FAILED
                    doc.error = "robots.txt disallow"
                    return doc
                await self._rate_limit(netloc)
                resp = await self._client.get(current)
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location", "")
                    if not location:
                        break
                    current = self._validate(urljoin(current, location))
                    doc.redirects.append(current)
                    continue
                doc.http_status = resp.status_code
                doc.final_url = str(resp.url)
                doc.domain = domain
                if resp.status_code >= 400:
                    doc.fetch_status = FetchStatus.FETCH_FAILED
                    doc.error = f"http {resp.status_code}"
                    return doc
                content_type = resp.headers.get("content-type", "")
                doc.content_type = content_type
                if "application/pdf" in content_type or current.lower().endswith(".pdf"):
                    text = extract_pdf(resp.content)
                    if text is None:
                        doc.fetch_status = FetchStatus.EXTRACTION_FAILED
                        doc.error = "pdf extraction failed"
                        return doc
                    doc.byte_len = len(resp.content)
                else:
                    text, meta = extract_html(resp.text, current)
                    doc.title = meta.get("title") or ""
                    doc.author = meta.get("author")
                    # published_at e' GIA' VALIDATA da extract.py (strutturata
                    # o plausibile non-bibliografica) oppure None: mai inventata
                    doc.published_at = meta.get("published_at")
                    set_dynamic_meta(doc, "published_at_validated",
                                     bool(doc.published_at))
                    # identita' bibliografica: DOI > PMID/PMCID > canonical > URL
                    ikey, ialias = identity_keys(meta, url, str(resp.url))
                    set_dynamic_meta(doc, "identity_key", ikey)
                    set_dynamic_meta(doc, "identity_aliases", list(ialias))
                    doc.byte_len = len(resp.content)
                    doc.text = text
                    # P1-8: links come from the RAW HTML (<a href>), not from
                    # the extracted text; kept as document metadata
                    from app.agent.snowball import extract_links as _xlinks
                    doc.links = _xlinks(resp.text, base_url=current, max_links=30)
                    break
            else:
                doc.fetch_status = FetchStatus.FETCH_FAILED
                doc.error = f"too many redirects (>={self._settings.fetch_max_redirects})"
                return doc
        except Exception as exc:
            doc.fetch_status = FetchStatus.FETCH_FAILED
            doc.error = f"{type(exc).__name__}: {exc}"
            return doc

        if not doc.text.strip():
            doc.fetch_status = FetchStatus.EXTRACTION_FAILED
            doc.error = "empty extraction"
        elif len(doc.text) < 200:
            doc.fetch_status = FetchStatus.PARTIAL_CONTENT
        else:
            doc.fetch_status = FetchStatus.SUCCESS
        if doc.fetch_status in (FetchStatus.SUCCESS, FetchStatus.PARTIAL_CONTENT):
            # dedup per contenuto: un errore di hash non deve mai bloccare il fetch
            try:
                doc.content_hash = content_fingerprint(doc.text)
            except Exception:
                doc.content_hash = ""
        self._to_cache(doc)
        return doc
