"""Fetcher: concurrent page download with SSRF guard, robots.txt, rate limit, cache."""
from __future__ import annotations

import asyncio
import logging
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
        return doc

    def _to_cache(self, doc: FetchedDocument) -> None:
        if doc.fetch_status in (FetchStatus.FETCH_FAILED,):
            return  # transient failures are not cached
        payload = doc.model_copy(update={"text": doc.text[:400_000]})
        self._cache.set_json("fetch", self._cache_key(doc.url),
                             payload.model_dump())

    async def _one(self, item: SearchResultItem, budget: RuntimeBudget | None,
                   on_event=None) -> FetchedDocument:
        try:
            url = self._validate(item.url)
        except Exception as exc:
            return FetchedDocument(url=item.url, fetch_status=FetchStatus.FETCH_FAILED,
                                   error=f"unsafe url: {exc}")
        if on_event:
            try:
                on_event("source_queued", url)
            except Exception:
                pass
        cached = self._from_cache(url)
        if cached is not None:
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
                return FetchedDocument(url=url, fetch_status=FetchStatus.FETCH_FAILED,
                                       error=f"not fetched, {exc}")
        async with self._sem:
            return await self._fetch_and_extract(url)

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
                    doc.published_at = meta.get("published_at")
                    doc.byte_len = len(resp.content)
                doc.text = text
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
        self._to_cache(doc)
        return doc
