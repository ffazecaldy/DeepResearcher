"""B4: snowballing — link citati dalle pagine accettate come candidate query.

Le pagine lette citano altre fonti: i loro link sonoCandidate di ricerca
" gratuite " (nessuna chiamata LLM). Vengono validati come gli URL di ricerca
(https/http pubblico, blocklist, niente re-read) e proposti al massimo N per run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from app.security import normalize_for_dedupe

# validate_public_http_url NON si usa qui: interroga il DNS (offline-unsafe).
# La validazione completa (SSRF, blocklist) avviene al fetch, come per ogni URL.

_A_HREF_RE = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"'#]+)[\"']", re.IGNORECASE)

_SCHEMES_OK = ("http://", "https://")


def extract_links(html: str, *, base_url: str = "",
                  max_links: int = 20) -> list[str]:
    """Estrae URL assoluti http/https dagli <a href> di una pagina.

    Risolve i link relativi con ``base_url``; scarta schemi non web
    (javascript:, mailto:, ftp:...), ancore e frammenti vuoti. Mai raises.
    """
    if not isinstance(html, str) or not html:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in _A_HREF_RE.findall(html):
        href = raw.strip()
        if not href:
            continue
        url = href
        if not url.lower().startswith(_SCHEMES_OK):
            if href.startswith(("#", "javascript:", "mailto:", "tel:", "ftp:")):
                continue
            if base_url:
                url = urljoin(base_url, href)
            else:
                continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max_links:
            break
    return out


@dataclass
class SnowballState:
    """Candidate URL raccolti dal testo delle pagine accettate (max per run)."""
    max_total: int = 3
    candidates: list[str] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    def propose(self, url: str) -> bool:
        """Registra un candidato; True se accettato (nuovo, valido, sotto cap).

        La validazione è SOLO sintattica (schema http/https, host presente,
        niente localhost/private-letterali): il DNS non viene interrogato qui
        (offline-safe); l'URL verrà comunque ri-validato dal searcher/fetcher.
        """
        if len(self.candidates) >= self.max_total:
            return False
        from urllib.parse import urlsplit
        import ipaddress as _ip
        try:
            url = url.strip()
            if not url.lower().startswith(_SCHEMES_OK):
                return False
            sp = urlsplit(url)
            host = (sp.hostname or "").lower()
            if not host or host == "localhost" or host.endswith(".localhost"):
                return False
            try:
                lit = _ip.ip_address(host)
                if not lit.is_global:
                    return False
            except ValueError:
                pass  # hostname: la validazione DNS avviene al fetch
            key = normalize_for_dedupe(url)
        except Exception:
            return False
        if key in self._seen:
            return False
        self._seen.add(key)
        self.candidates.append(url)
        return True

    def take_pending(self, already_seen_norm: set[str]) -> list[str]:
        """Candidate ancora utili (mai visti nel run), in ordine FIFO."""
        out: list[str] = []
        for url in self.candidates:
            if normalize_for_dedupe(url) in already_seen_norm:
                continue
            out.append(url)
        return out
