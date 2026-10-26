"""Regression: parser DDG separa organici da sponsorizzati.

Un risultato con URL destinato ad annuncio (duckduckgo.com/y.js con
ad_domain/ad_provider) NON deve essere registrato come documento: se la
destinazione reale (u3 base64url) e' valida viene usata quella, altrimenti il
risultato e' scartato. I parametri di tracking (utm_*, gclid) vengono spogliati
dai risultati organici.
"""
from __future__ import annotations

import base64
from urllib.parse import quote

from app.clients.search.duckduckgo import (
    _is_ad_redirect,
    _is_valid_http,
    _parse_results,
    _resolve_result_url,
    _strip_ad_tracking_params,
)

_ORGANIC_TARGET = ("https://istituto-example.org/pagine/ricerca"
                   "?utm_source=ddg&utm_medium=serp&gclid=CjwK")
_ORGANIC_WRAPPED = ("https://duckduckgo.com/l/?uddg="
                    + quote(_ORGANIC_TARGET, safe="") + "&rut=abc123")

_AD_TARGET = "https://sponsor-example.com/offerta"
_AD_RESOLVABLE = ("https://duckduckgo.com/y.js?ad_domain=sponsor-example.com"
                  "&ad_provider=bing&ad_provider_id=0001&u3="
                  + base64.urlsafe_b64encode(_AD_TARGET.encode()).decode())

_AD_NO_TARGET = ("https://duckduckgo.com/y.js?ad_domain=ads-example.net"
                 "&ad_provider=bing&u3=%21%21non%20un%20url%21%21")

_AD_NETWORK_NO_TARGET = ("https://www.googleadservices.com/pagead/aclk"
                         "?sa=L&ai=xyz")

_AFFILIATE_DIRECT = ("https://shop-example.com/prodotto"
                     "?affid=42&utm_campaign=serp")


def _anchor(href: str, title: str) -> str:
    return f'<a rel="nofollow" class="result__a" href="{href}">{title}</a>'


def _serp() -> str:
    return ("<div class='results'>"
            + _anchor(_ORGANIC_WRAPPED, "Studio istituzionale")
            + _anchor(_AD_RESOLVABLE, "Offerta sponsorizzata")
            + _anchor(_AD_NO_TARGET, "Annuncio senza destinazione")
            + _anchor(_AD_NETWORK_NO_TARGET, "Annuncio ad network")
            + _anchor(_AFFILIATE_DIRECT, "Affiliazione")
            + "</div>")


def test_parser_separates_ads_from_organic():
    results = _parse_results(_serp(), max_results=10)
    urls = [r.url for r in results]

    # NESSUN URL pubblicitario registrato come documento
    assert all("duckduckgo.com/y.js" not in u for u in urls)
    assert all("googleadservices.com" not in u for u in urls)
    assert all("ad_domain=" not in u for u in urls)

    # sponsorizzato CON destinazione reale valida -> viene estratta quella
    assert _AD_TARGET in urls

    # annunci SENZA destinazione valida -> scartati del tutto
    assert len(results) == 3
    assert "https://ads-example.net" not in "".join(urls)

    # organico: wrapper /l/?uddg= rimosso e tracking (utm_*, gclid) spogliato
    organic = results[0]
    assert organic.url == "https://istituto-example.org/pagine/ricerca"
    assert organic.title == "Studio istituzionale"

    # affiliazione su host normale: resta il documento, senza parametri
    affiliate = [r for r in results if "shop-example.com" in r.url][0]
    assert affiliate.url == "https://shop-example.com/prodotto"

    # posizioni riassegnate in sequenza
    assert [r.position for r in results] == [1, 2, 3]


def test_ad_redirect_detection():
    assert _is_ad_redirect(_AD_RESOLVABLE)
    assert _is_ad_redirect(_AD_NO_TARGET)
    assert _is_ad_redirect("https://example.com/page?ad_domain=x")
    assert _is_ad_redirect("https://example.com/page?affid=1")
    assert _is_ad_redirect("https://www.awin1.com/cread.php?page=x")
    assert not _is_ad_redirect("https://istituto-example.org/pagine/ricerca")
    assert not _is_ad_redirect("https://duckduckgo.com/l/?uddg="
                               + quote(_ORGANIC_TARGET, safe=""))


def test_resolver_and_strip_helpers():
    # il resolver non registra MAI l'URL dell'annuncio
    assert _resolve_result_url(_AD_RESOLVABLE) == _AD_TARGET
    assert _resolve_result_url(_AD_NO_TARGET) is None
    assert _resolve_result_url(_AD_NETWORK_NO_TARGET) is None
    assert _resolve_result_url(_ORGANIC_WRAPPED) == \
        "https://istituto-example.org/pagine/ricerca"
    # tutti i parametri sono tracking -> query vuota, non parametri conservati
    assert _strip_ad_tracking_params(
        "https://shop-example.com/prodotto?utm_source=x&gclid=y") == \
        "https://shop-example.com/prodotto"
    assert _is_valid_http("https://ok.example.com/a") is True
    assert _is_valid_http("not-a-url") is False
