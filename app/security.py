"""URL validation (SSRF guard), URL normalization/dedup, untrusted-content delimiting."""
from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "twclid", "igshid", "li_fat_id",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "pk_campaign", "pk_kwd",
    "piwik_campaign", "piwik_kwd", "mtm_campaign", "mtm_source", "mtm_medium",
    "spm", "ref_src", "ref_url", "ved", "ei",
})

_EXT_OPEN = "<external_web_content>"
_EXT_CLOSE = "</external_web_content>"


class UnsafeUrlError(ValueError):
    """URL refused by the SSRF/domain policy."""


_DNS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DNS_TTL_S = 300.0


def _resolve(host: str) -> list[str]:
    now = time.monotonic()
    hit = _DNS_CACHE.get(host)
    if hit and now - hit[0] < _DNS_TTL_S:
        return hit[1]
    infos = socket.getaddrinfo(host, None)
    addrs = [sockaddr[0] for _fam, _typ, _proto, _canon, sockaddr in infos]
    _DNS_CACHE[host] = (now, addrs)
    return addrs


def _assert_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        raise UnsafeUrlError(f"non-public IP refused: {ip}")


def validate_public_http_url(url: str, *, blocked_domains: frozenset[str] = frozenset(),
                             allow_private: bool = False) -> str:
    """Return the URL unchanged if safe to fetch; raise UnsafeUrlError otherwise.

    allow_private=True skips the IP checks (TEST-ONLY escape hatch for loopback
    fake servers); scheme/host rules still apply.
    """
    sp = urlsplit(url.strip())
    if sp.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"scheme not allowed: {sp.scheme!r}")
    host = (sp.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("missing host")
    if host == "localhost" or host.endswith(".localhost") or host in blocked_domains:
        raise UnsafeUrlError(f"host not allowed: {host}")
    if allow_private:
        return url.strip()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _assert_public_ip(literal)
    else:
        for addr in _resolve(host):
            _assert_public_ip(ipaddress.ip_address(addr.split("%")[0]))
    return url.strip()


def strip_tracking(url: str) -> str:
    """Remove known tracking parameters from a URL."""
    sp = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
         if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), ""))


def normalize_for_dedupe(url: str) -> str:
    """Canonical dedup key: https, no www., no tracking, sorted query, no fragment."""
    sp = urlsplit(strip_tracking(url))
    host = (sp.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = sp.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = sp.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    q = sorted(parse_qsl(sp.query, keep_blank_values=True))
    return urlunsplit(("https", netloc, path, urlencode(q), ""))


def wrap_external(text: str, max_chars: int = 16000) -> str:
    """Delimit untrusted web content for prompts; neutralize delimiter escapes."""
    t = text[:max_chars].replace(_EXT_CLOSE, "<\\/external_web_content>")
    return f"{_EXT_OPEN}\n{t}\n{_EXT_CLOSE}"
