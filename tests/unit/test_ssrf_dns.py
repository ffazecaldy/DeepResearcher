"""Deterministic SSRF/DNS tests: the injected resolver removes the real-DNS
dependency that made 3 audit tests environment-dependent; the production
protection is NOT weakened (same code path, only resolution is injectable)."""
from __future__ import annotations

import pytest

from app import security
from app.security import validate_public_http_url


@pytest.fixture()
def fake_dns(monkeypatch):
    """Inject a deterministic resolver; restore after the test."""
    def _install(mapping: dict[str, list[str]]):
        monkeypatch.setattr(security, "_DNS_RESOLVER",
                            lambda host: mapping.get(host, ["93.184.216.34"]))
        security._DNS_CACHE.clear()
    return _install


def test_public_domain_resolved_public_passes(fake_dns):
    fake_dns({"good.example": ["93.184.216.34"]})
    assert validate_public_http_url(
        "https://good.example/x", allow_private=False) == "https://good.example/x"


def test_domain_resolving_private_is_blocked(fake_dns):
    fake_dns({"evil.example": ["10.0.0.5"]})
    with pytest.raises(Exception):
        validate_public_http_url("https://evil.example/x", allow_private=False)


def test_dns_rebinding_second_lookup_private_is_blocked(fake_dns):
    """Rebinding simulation: first lookup public, cached; the guard uses the
    same resolved set -> a rebinding to a private IP is caught at fetch time
    by re-validation on the FINAL url (the fetcher revalidates per hop)."""
    calls = {"n": 0}

    def resolver(host):
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["192.168.1.1"]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(security, "_DNS_RESOLVER", resolver)
    security._DNS_CACHE.clear()
    try:
        assert validate_public_http_url("https://rb.example/a")  # first: public
        with pytest.raises(Exception):
            validate_public_http_url("https://rb.example/b")  # now private
    finally:
        monkeypatch.undo()


def test_literal_private_ip_still_blocked_without_dns(fake_dns):
    fake_dns({})  # resolver never called for literals
    with pytest.raises(Exception):
        validate_public_http_url("http://127.0.0.1/x", allow_private=False)
    with pytest.raises(Exception):
        validate_public_http_url("http://169.254.169.254/latest/meta-data/",
                                 allow_private=False)
