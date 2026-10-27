"""HTML main-content extraction with metadata.

Primary: trafilatura (bare_extraction with metadata).
Fallback: readability-lxml when the trafilatura text is missing or shorter
than ``_FALLBACK_THRESHOLD`` chars. Never raises: on total failure returns
``("", meta)`` with at least the title from ``<title>``.

IDENTITA' BIBLIOGRAFICA: ``meta`` porta sempre le chiavi ``doi``, ``pmid``,
``pmcid``, ``canonical_url`` (dal <head>: meta citation_*/dc.*, link rel=
canonical, pattern doi.org e URL NCBI). Il fetcher le trasforma in
``identity_key`` per la dedup per identita' (PubMed e PMC dello stesso studio
hanno testo estratto diverso ma lo stesso DOI).

DATE: ``published_at`` e' SEMPRE una data validata o None — mai inventata.
Preferenza alle sorgenti strutturate (JSON-LD datePublished, meta
citation_*/dc.date); una data trovata solo dentro righe di bibliografia
(authors + journal abbreviato) viene scartata; plausibilita' 1900..oggi+1.
La data di RECUPERO della pagina e' un'altra cosa (``fetched_at`` del doc).
"""
from __future__ import annotations

import html as _html
import json as _json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import trafilatura

log = logging.getLogger(__name__)

# Below this length the readability-lxml fallback is attempted (spec: 200).
_FALLBACK_THRESHOLD = 200

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def extract_html(html: str, url: str | None = None) -> tuple[str, dict[str, Any]]:
    """Extract main text and metadata from an HTML string.

    Returns ``(text, meta)`` where ``meta = {"title", "author", "published_at",
    "doi", "pmid", "pmcid", "canonical_url"}`` (values are ``str | None``;
    ``published_at`` e' validata: data plausibile normalizzata o None).
    Never raises.
    """
    meta: dict[str, Any] = {"title": None, "author": None, "published_at": None,
                            "doi": None, "pmid": None, "pmcid": None,
                            "canonical_url": None}
    if not isinstance(html, str) or not html.strip():
        return "", meta

    # Baseline title straight from <title>; overridden by richer metadata below.
    meta["title"] = _title_from_source(html)
    # Identita' bibliografica (DOI/PMID/PMCID/canonical) dal <head> + URL.
    meta.update(bibliographic_identity(html, url))
    # Data ACCETTABILE da sorgenti strutturate: vince su qualsiasi candidata
    # text-level (trafilatura puo' pescare anni dalla bibliografia interna).
    trusted = _trusted_date(html)

    text = _via_trafilatura(html, url, meta)
    if text is None or len(text) < _FALLBACK_THRESHOLD:
        alt = _via_readability(html, meta)
        if alt and (text is None or len(alt) > len(text)):
            text = alt

    meta["published_at"] = _resolve_published_at(
        html, meta.get("published_at"), trusted)
    if not text:
        return "", meta
    return _tidy(text), meta


def _via_trafilatura(
    html: str, url: str | None, meta: dict[str, Any]
) -> str | None:
    try:
        doc = trafilatura.bare_extraction(
            html, url=url, with_metadata=True, output_format="python"
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        log.debug("trafilatura failed on %s: %s", url or "<inline>", exc)
        return None
    if doc is None:
        return None

    if hasattr(doc, "as_dict"):
        data = doc.as_dict()
    elif isinstance(doc, dict):
        data = doc
    else:  # unexpected shape: pull the known attributes defensively
        data = {k: getattr(doc, k, None) for k in ("text", "title", "author", "date")}

    if meta["title"] is None and data.get("title"):
        meta["title"] = _clean(data["title"])
    if data.get("author"):
        meta["author"] = _clean(data["author"])
    if data.get("date"):
        meta["published_at"] = _clean(data["date"])
    return (data.get("text") or "").strip() or None


def _via_readability(html: str, meta: dict[str, Any]) -> str | None:
    try:
        import readability

        doc = readability.Document(html)
        summary = doc.summary(html_partial=False)
        text = _strip_tags(summary).strip()
        if not text:
            return None
        if meta["title"] is None:
            try:
                meta["title"] = _clean(doc.short_title())
            except Exception:  # noqa: BLE001
                pass
        return text
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        log.debug("readability fallback failed: %s", exc)
        return None


def _title_from_source(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    if not m:
        return None
    return _clean(_html.unescape(_strip_tags(m.group(1))))


def _strip_tags(fragment: str) -> str:
    try:
        from lxml import html as lxml_html

        return lxml_html.fromstring(fragment).text_content()
    except Exception:  # noqa: BLE001 - malformed fragment: regex fallback
        return _html.unescape(_TAG_RE.sub(" ", fragment))


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = _SPACES_RE.sub(" ", str(value)).strip()
    return s or None


def _tidy(text: str) -> str:
    """Collapse horizontal whitespace per line; keep paragraph newlines."""
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


# ---------- identita' bibliografica (DOI / PMID / PMCID / canonical) ----------
_HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_ATTR_RE = re.compile(
    r"""(\bname\b|\bproperty\b|\bcontent\b)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE)
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_LINK_CANONICAL_RE = re.compile(
    r"""rel\s*=\s*["']canonical["']""", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_LDJSON_RE = re.compile(
    r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
_DOI_ORG_RE = re.compile(
    r"https?://(?:dx\.)?doi\.org/(\S+?)(?=[\"'<>\s]|$)", re.IGNORECASE)
_PMCID_RE = re.compile(r"\bPMC\d{6,10}\b", re.IGNORECASE)
_PMC_URL_RE = re.compile(
    r"(?:www\.)?ncbi\.nlm\.nih\.gov/pmc/articles/(PMC\d+)", re.IGNORECASE)
_PMID_URL_RE = re.compile(
    r"(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,10})", re.IGNORECASE)

# meta date "trusted": sorgenti strutturate, mai testo di bibliografia
_META_DATE_KEYS = (
    "citation_publication_date", "citation_date", "dc.date", "dc.date.issued",
    "article:published_time", "published_time", "pubdate", "date",
    "parsely-pub-date", "sailthru.date",
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%d %b %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m", "%Y/%m",
)
_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
# euristica bibliografia: "Benjamini Y," / "Hochberg Y." / "et al"
_BIB_AUTHORS_RE = re.compile(r"\bet[.\s]+al\b\.?|\b[A-Z][\w'’-]{1,20}\s+[A-Z]\b")
_BIB_VOLPAGE_RE = re.compile(r"\b\d{1,5}\(\d{1,5}\)[:;]?\d*|\b\d{1,5}:\s*\d{1,6}")
_BIB_JABBR_RE = re.compile(r"(?:\b[A-Z]\b\.?\s+){1,2}[A-Z][a-z]{2,}")


def _head(html: str) -> str:
    """Il <head> del documento (i meta tag fuori head non sono affidabili:
    la bibliografia interna di una pagina puo' contenere DOI/date di ALTRI
    lavori e non deve mai diventare identita' di QUESTA pagina)."""
    m = _HEAD_END_RE.search(html)
    return html[:m.end()] if m else html


def _meta_map(fragment: str) -> dict[str, str]:
    """meta name/property -> content (chiavi lowercase, primo valore vince)."""
    out: dict[str, str] = {}
    for tag in _META_TAG_RE.findall(fragment):
        attrs: dict[str, str] = {}
        for m in _META_ATTR_RE.finditer(tag):
            value = m.group(2) if m.group(2) is not None else m.group(3)
            attrs[m.group(1).lower()] = _html.unescape(value or "").strip()
        name = attrs.get("name") or attrs.get("property")
        content = attrs.get("content")
        if name and content is not None:
            out.setdefault(name.lower(), content)
    return out


def _norm_doi(raw: str | None) -> str | None:
    """DOI normalizzato (lowercase, senza prefissi doi:/doi.org) o None."""
    if not raw:
        return None
    s = _html.unescape(str(raw)).strip()
    m = _DOI_ORG_RE.search(s)
    if m:
        s = m.group(1)
    m = _DOI_RE.search(s.strip())
    if not m:
        return None
    doi = m.group(1).rstrip(".,;)")
    return doi.lower() or None


def _norm_pmid(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.search(r"\b(\d{5,10})\b", str(raw))
    return m.group(1) if m else None


def _norm_pmcid(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _PMCID_RE.search(str(raw))
    return m.group(0).upper() if m else None


def bibliographic_identity(html: str, url: str | None = None) -> dict[str, str]:
    """Identita' bibliografica della PAGINA: doi / pmid / pmcid / canonical_url.

    Solo dal <head> (meta citation_*/dc.*, link rel=canonical, pattern
    doi.org) piu' fallback dall'URL della pagina (pubmed.ncbi.nlm.nih.gov/<pmid>,
    ncbi.nlm.nih.gov/pmc/articles/<pmcid>). Il corpo NON viene scandito: la
    bibliografia interna contiene DOI/PMID di ALTRI lavori.
    """
    head = _head(html)
    meta = _meta_map(head)
    doi = (_norm_doi(meta.get("citation_doi")) or _norm_doi(meta.get("dc.doi"))
           or _norm_doi(meta.get("doi")))
    if doi is None:
        m = _DOI_ORG_RE.search(head)
        doi = _norm_doi(m.group(0)) if m else None
    pmid = _norm_pmid(meta.get("citation_pmid"))
    pmcid = _norm_pmcid(meta.get("citation_pmcid"))
    canonical_url = ""
    for tag in _LINK_TAG_RE.findall(head):
        if _LINK_CANONICAL_RE.search(tag):
            m = _HREF_RE.search(tag)
            if m:
                canonical_url = _html.unescape(m.group(1)).strip()
            break
    if url:
        if not pmcid:
            m = _PMC_URL_RE.search(url)
            if m:
                pmcid = m.group(1).upper()
        if not pmid:
            m = _PMID_URL_RE.search(url)
            if m:
                pmid = m.group(1)
    return {"doi": doi, "pmid": pmid, "pmcid": pmcid,
            "canonical_url": canonical_url or None}


# ---------- validazione date ----------
def _plausible(year: int, full: date | None = None) -> bool:
    """Plausibilita': 1900 <= anno; futuro al massimo oggi+1 anno."""
    if year < 1900:
        return False
    today = date.today()
    if full is not None:
        return full <= today + timedelta(days=366)
    return year <= today.year + 1


def validate_date(value: Any) -> str | None:
    """Data plausibile -> 'YYYY-MM-DD' (o 'YYYY'/'YYYY-MM' se parziale).

    Accetta ISO, yyyy/mm/dd, testuali ("March 5, 2021"), datetime ISO con
    tempo/timezone, anno secco. Non plausibile o non interpretabile -> None
    (la data non viene mai inventata).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if m.group(3):
            da = int(m.group(3))
            try:
                d = date(y, mo, da)
            except ValueError:
                return None
            return (f"{y:04d}-{mo:02d}-{da:02d}"
                    if _plausible(y, d) else None)
        if 1 <= mo <= 12 and _plausible(y):
            return f"{y:04d}-{mo:02d}"
        return None
    if re.fullmatch(r"\d{4}", s):
        y = int(s)
        return s if _plausible(y) else None
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        return d.isoformat() if _plausible(d.year, d) else None
    m = _YEAR_RE.search(s)
    if m and _plausible(int(m.group(1))):
        return m.group(1)
    return None


def _jsonld_date_published(fragment: str) -> str | None:
    """Prima ``datePublished`` valida nei blocchi JSON-LD (@graph e liste
    incluse). Solo sorgenti strutturate: mai testo libero."""
    for m in _LDJSON_RE.finditer(fragment):
        try:
            data = _json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key in ("datePublished", "dateCreated"):
                    v = validate_date(node.get(key))
                    if v:
                        return v
                stack.extend(v for v in node.values()
                             if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return None


def _trusted_date(html: str) -> str | None:
    """Data di pubblicazione ACCETTABILE: JSON-LD datePublished o meta
    citation_*/dc.date, in ordine di priorita'. None se nessuna e' valida."""
    head = _head(html)
    meta = _meta_map(head)
    candidates = [_jsonld_date_published(html)]
    candidates.extend(meta.get(k) for k in _META_DATE_KEYS)
    for cand in candidates:
        v = validate_date(cand)
        if v:
            return v
    return None


def _bibliography_line(line: str) -> bool:
    """Euristica: la riga e' una voce di bibliografia?Serve la CONGIUNZIONE
    di (a) piu' autori ("Cognome X, Cognome Y." / "et al") e (b) journal
    abbreviato ("J R Stat Soc B") o volume:pagine ("57(1):289")."""
    if not _YEAR_RE.search(line):
        return False
    low = line.lower()
    authors = "et al" in low or len(_BIB_AUTHORS_RE.findall(line)) >= 2
    if not authors:
        return False
    return bool(_BIB_JABBR_RE.search(line) or _BIB_VOLPAGE_RE.search(low))


def _only_in_bibliography(html: str, year: str) -> bool:
    """True se OGNI occurrence dell'anno nel sorgente sta in righe da
    bibliografia (caso trafilatura+PubMed: l'anno era di una citazione
    interna, es. 'Benjamini 1995', non della pagina). Se l'anno non compare
    nel sorgente (veniva da un meta tag scartato dallo split) non si scarta."""
    chunks = [c.strip() for c in
              re.split(r"<[^>]+>|\r\n|\r|\n", html) if c.strip()]
    hits = [c for c in chunks if year in c]
    if not hits:
        return False
    return all(_bibliography_line(c) for c in hits)


def _resolve_published_at(html: str, candidate: Any,
                          trusted: str | None) -> str | None:
    """Risoluzione finale di published_at (nessuna data inventata, mai
    confusa con la data di recupero ``fetched_at``):
    1. sorgente strutturata (JSON-LD / meta citation_*): vince sempre;
    2. candidata trafilatura: accettata solo se plausibile E non ricavata
       dalla sola bibliografia interna;
    3. altrimenti None.
    """
    if trusted:
        return trusted
    norm = validate_date(candidate)
    if not norm:
        return None
    year = norm[:4]
    if _only_in_bibliography(html, year):
        return None
    return norm
