"""Dedup per IDENTITA' BIBLIOGRAFICA (DOI/PMID/PMCID/canonical) — B14.

Regressione del bug reale: PubMed e PMC dello stesso studio hanno testo
estratto diverso (content_hash diverso) -> la dedup per contenuto non li
univa e la copia veniva VALUTATA e LETTA comunque (17 evidenze dallo stesso
identico contenuto). Ora:

- identity_key/identity_aliases (convenzione fetcher: attributi dinamici,
  letti con getattr) uniscono le copie bibliografiche;
- l'orchestrator filtra i duplicati (content E identity) PRIMA di
  _evaluate_docs e _read_docs: non valutati, non letti, non contati.

Offline: fakes per LLM/search/fetcher, zero rete, zero API key.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

from app.agent.fetcher import (content_fingerprint, ensure_dynamic_meta,
                               identity_keys, set_dynamic_meta)
from app.extraction.extract import extract_html
from app.agent.gap_checker import GapChecker
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.agent.reader import Reader
from app.agent.verifier import Verifier
from app.agent.writer import Writer
from app.config import Depth, Settings
from app.decision.base import DecisionRouter
from app.decision.llm_fallback import LLMDecisionEngine
from app.events.bus import EventBus
from app.models import (DecisionKind, DecisionResult, DecisionSpec,
                        FetchedDocument, FetchStatus, SearchResultItem,
                        SubQuestion)
from app.storage import Storage
from tests.fakes import FakeLLMClient

DOI = "10.1101/2020.11.05.123456"

PUBMED_HTML = """<html><head><title>Studio clinico COVID</title>
<meta name="citation_pmid" content="33201262">
<meta name="citation_doi" content="https://doi.org/10.1101/2020.11.05.123456">
<meta name="citation_date" content="2020/11/17"></head><body>
<p>Risultati dello studio clinico condotto su 480 pazienti ricoverati.
""" + ("La coorte evidenzia una riduzione significativa della carica virale "
       "nei pazienti trattati con remdesivir rispetto al controllo. ") * 3 + """
</p><div class="references">Benjamini Y, Hochberg Y. Controlling the false
discovery rate. J R Stat Soc B 1995;57(1):289-300.</div></body></html>"""

PMC_HTML = """<html><head><title>Studio clinico COVID - full text</title>
<meta name="citation_doi" content="10.1101/2020.11.05.123456">
<meta name="citation_pmcid" content="PMC7719368"></head><body>
<p>Testo integrale con sezioni metodi e risultati, completamente diverso
dall'abstract della pagina PubMed.
""" + ("I pazienti sono stati randomizzati in due bracci e seguiti per "
       "ventotto giorni con valutazioni giornaliere della gravita clinica. ") * 3 + """
</p></body></html>"""


# ---------- identity_key: estrazione e priorita' ----------
def _meta_of(html: str, url: str) -> dict:
    _text, meta = extract_html(html, url)
    return meta


def test_pubmed_pmc_same_doi_same_identity_key():
    """Requisito B14: PubMed e PMC dello stesso studio -> identity_key UGUALE
    (il DOI e' la priorita' 1); le alias si intersecano anche quando una
    pagina non porta il DOI (match per PMID)."""
    meta_pm = _meta_of(PUBMED_HTML, "https://pubmed.ncbi.nlm.nih.gov/33201262/")
    meta_pmc = _meta_of(PMC_HTML, "https://pmc.ncbi.nlm.nih.gov/articles/PMC7719368/")
    assert meta_pm["doi"] == DOI
    assert meta_pmc["doi"] == DOI  # PMC page: stesso DOI in citation_doi
    assert meta_pm["pmid"] == "33201262"

    k_pm, a_pm = identity_keys(meta_pm, "https://pubmed.ncbi.nlm.nih.gov/33201262/")
    k_pmc, a_pmc = identity_keys(meta_pmc,
                                 "https://pmc.ncbi.nlm.nih.gov/articles/PMC7719368/")
    assert k_pm == k_pmc == f"doi:{DOI}"
    assert a_pm[0] == a_pmc[0] == f"doi:{DOI}"
    assert set(a_pm) != set(a_pmc)  # alias diverse, ma la primaria collide

    # variante: pagina PMC SENZA citation_doi ma con citation_pmid -> collide
    # comunque via alias PMID (contenuto diverso, identita' uguale)
    meta_pmc2 = dict(meta_pmc, doi=None, pmid="33201262")
    k2, a2 = identity_keys(meta_pmc2, "https://pmc.ncbi.nlm.nih.gov/articles/PMC7719368/")
    assert k2 == "pmid:33201262"
    assert set(a2) & set(a_pm) == {"pmid:33201262"}


def test_identity_keys_priority():
    """Priorita' DOI > PMID > PMCID > canonical > URL normalizzato."""
    full = identity_keys({"doi": "10.1234/X", "pmid": "111", "pmcid": "PMC222",
                          "canonical_url": "https://j.example/art"},
                         "https://cdn.example/mirror")
    assert full[0] == "doi:10.1234/x"  # DOI normalizzato lowercase
    assert full[1][:4] == ["doi:10.1234/x", "pmid:111", "pmc:PMC222",
                           "canon:https://j.example/art"]

    ids_only = identity_keys({"pmid": "", "pmcid": "pmc222"}, "https://a.example/p")
    assert ids_only[0] == "pmc:PMC222"

    canon_only = identity_keys({"canonical_url": "https://j.example/art"},
                               "https://amp.example/art")
    assert canon_only[0] == "canon:https://j.example/art"

    url_only = identity_keys({}, "https://b.example/page?q=1")
    assert url_only[0].startswith("url:https://b.example/page")

    empty = identity_keys({}, "")
    assert empty == ("", [])


def test_dynamic_meta_convention():
    """Convenzione documentata nel fetcher: attributi dinamici su
    FetchedDocument (models.py non estendibile), default via helper."""
    doc = FetchedDocument(url="https://x.example/a",
                          fetch_status=FetchStatus.SUCCESS)
    ensure_dynamic_meta(doc)
    assert getattr(doc, "identity_key", "MISSING") == ""
    assert getattr(doc, "identity_aliases", "MISSING") == []
    assert getattr(doc, "published_at_validated", "MISSING") is False
    set_dynamic_meta(doc, "identity_key", "doi:" + DOI)
    assert getattr(doc, "identity_key") == "doi:" + DOI
    # fuori schema: non compaiono nel dump del modello
    assert "identity_key" not in doc.model_dump()


# ---------- storage: colonna identity_key ----------
def test_storage_identity_key_roundtrip_and_scope(tmp_path):
    s = Storage(os.path.join(tmp_path, "r.db"))
    s.create_run("run1", "q?", "it", "standard")
    s.create_run("run2", "q?", "it", "standard")
    s.add_source("src1", "run1", "https://pub.example/1", "", "pub.example",
                 "T", 200, "text/html", "SUCCESS", None, None, [], None, 500,
                 content_hash="h1", identity_key=f"doi:{DOI}")
    # add_source senza identity_key: default "" (back-compat)
    s.add_source("src2", "run1", "https://b.example/2", "", "b.example",
                 "T", 200, "text/html", "SUCCESS", None, None, [], None, 500)
    assert s.identity_seen("run1", f"doi:{DOI}") is True
    assert s.identity_seen("run1", f"doi:{DOI}",
                           exclude_source_id="src1") is False
    assert s.identity_seen("run2", f"doi:{DOI}") is False  # scope: stesso run
    assert s.identity_seen("run1", "") is False            # vuota mai dedup
    assert s.identity_seen_any("run1", ["x", f"doi:{DOI}"]) is True
    assert s.identity_seen_any("run1", ["x", "y"]) is False
    assert s.identity_seen_any("run1", []) is False
    rows = s.sources_for_run("run1")
    assert rows[0]["identity_key"] == f"doi:{DOI}"
    assert rows[1]["identity_key"] == ""
    s.close()


def test_migration_adds_identity_key_to_existing_db(tmp_path):
    """DB creato con lo schema vecchio: colonna aggiunta in modo idempotente,
    i dati sopravvivono e le fonti vecchie hanno identity_key vuota."""
    db_path = os.path.join(tmp_path, "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
CREATE TABLE sources(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, url TEXT, url_final TEXT, domain TEXT,
  title TEXT, http_status INTEGER, content_type TEXT, fetch_status TEXT,
  published_at TEXT, author TEXT, redirects_json TEXT, error TEXT, byte_len INTEGER,
  content_hash TEXT DEFAULT '', source_type TEXT, quality_score INTEGER,
  relevance REAL, accepted INTEGER DEFAULT 0);
INSERT INTO sources(id, run_id, url, byte_len)
  VALUES('old1', 'run0', 'https://old.example.com', 42);
""")
    conn.commit()
    conn.close()

    s = Storage(db_path)   # prima apertura: migra
    s.close()
    s = Storage(db_path)   # seconda apertura: idempotente
    try:
        cols = {r["name"] for r in s._query("PRAGMA table_info(sources)")}
        assert "identity_key" in cols
        row = s._query("SELECT id, identity_key FROM sources WHERE id='old1'")[0]
        assert row["identity_key"] == ""
        s.add_source("new1", "run0", "https://n.example.com", "", "n.example.com",
                     "T", 200, "text/html", "SUCCESS", None, None, [], None, 7,
                     identity_key=f"pmid:33201262")
        assert s.identity_seen("run0", "pmid:33201262") is True
    finally:
        s.close()


# ---------- orchestrator: il duplicato NON viene piu' valutato/letto ----------
T_PUBMED = ("Studio clinico su 480 pazienti ricoverati con riduzione della "
            "carica virale nei trattati con remdesivir. " * 5)
T_PMC = ("Randomizzazione in due bracci con follow-up di ventotto giorni e "
         "valutazioni giornaliere della gravita clinica dei pazienti. " * 5)
T_RADIO = ("La radio arrivò nelle case italiane negli anni venti del "
           "novecento con trasmissioni regolari. " * 5)

PLAN_JSON = {
    "title": "Remdesivir e carica virale",
    "language": "it",
    "subquestions": [
        {"subquestion_id": "x1", "text": "Il remdesivir riduce la carica virale?"},
        {"subquestion_id": "x2", "text": "Quando è arrivata la radio?"},
    ],
    "queries": [{"subquestion_id": "x1", "text": "remdesivir carica virale"}],
}
READ_JSON = {"evidences": [
    {"claim": "Il trattamento riduce la carica virale",
     "quote": "riduzione significativa della carica virale"},
]}
GAP_JSON = {"new_queries": [], "contradictions": []}


def _writer_fn(system, user, force_json):
    eids = __import__("re").findall(r'"evidence_id":"(ev_[a-z0-9]+)"', user)
    if not eids:
        eids = __import__("re").findall(r'"evidence_id":\s*"(ev_[a-z0-9]+)"', user)
    return json.dumps({
        "title": "Remdesivir e carica virale",
        "markdown": "Il trattamento riduce la carica virale nei pazienti.",
        "claims": [{"text": "Il trattamento riduce la carica virale.",
                    "evidence_ids": eids[:1]}],
    })


def _verifier_fn(system, user, force_json):
    return json.dumps({"verdict": "VERIFIED", "support": "supported",
                       "reason": "quote matches"})


class ScriptedSearcher:
    def __init__(self, per_cycle: list[list[str]]):
        self.per_cycle = per_cycle
        self.calls = 0

    async def run_queries(self, queries, max_pages_per_query):
        urls = self.per_cycle[min(self.calls, len(self.per_cycle) - 1)]
        self.calls += 1
        out = []
        for i, url in enumerate(urls):
            qid = queries[0].query_id if queries else "q"
            out.append(SearchResultItem(query_id=qid, url=url,
                                        title=f"risultato {i}", position=i))
        return out


class ScriptedFetcher:
    """Fetch senza rete: url -> doc pre-costruito CON attributi dinamici
    identity_key/identity_aliases/published_at_validated (convenzione fetcher)."""

    def __init__(self, specs: dict[str, dict]):
        self.specs = specs
        self.read_urls: list[str] = []

    async def fetch_all(self, items, budget=None, on_event=None):
        docs = []
        for it in items:
            spec = self.specs[it.url]
            doc = FetchedDocument(
                source_id=spec["source_id"], url=it.url, final_url=it.url,
                domain=spec["domain"], title=spec.get("title", ""),
                text=spec["text"], http_status=200, content_type="text/html",
                published_at=spec.get("published_at"),
                fetch_status=FetchStatus.SUCCESS,
                content_hash=content_fingerprint(spec["text"]))
            set_dynamic_meta(doc, "identity_key", spec["identity_key"])
            set_dynamic_meta(doc, "identity_aliases", list(spec["identity_aliases"]))
            set_dynamic_meta(doc, "published_at_validated",
                             bool(spec.get("published_at")))
            docs.append(doc)
        return docs


class CountingDecisionEngine:
    """Tiene traccia dei documenti effettivamente VALUTATI."""
    name = "fake"

    def __init__(self):
        self.evaluated: list[str] = []

    async def decide(self, spec, state, *, subject_kind="", subject_id=""):
        return (await self.decide_many([spec], state, subject_kind=subject_kind,
                                       subject_id=subject_id))[0]

    async def decide_many(self, specs, state, *, subject_kind="", subject_id=""):
        if subject_kind == "source":
            self.evaluated.append(subject_id)
        out = []
        for s in specs:
            value = {DecisionKind.BINARY: True, DecisionKind.CHOICE: "paper",
                     DecisionKind.SCORE: 2}[s.kind]
            out.append(DecisionResult(name=s.name, kind=s.kind, engine=self.name,
                                      value=value, confidence=0.9,
                                      subject_kind=subject_kind,
                                      subject_id=subject_id))
        return out


class CountingReader:
    def __init__(self):
        self.calls = 0

    async def extract_evidences(self, question, subquestion_text, chunk,
                                max_evidences=4, subquestion_id=None,
                                doc_title=None):
        self.calls += 1
        from app.models import Evidence
        return [Evidence(source_id=chunk.source_id, chunk_id=chunk.chunk_id,
                         quote="riduzione significativa della carica virale",
                         claim="Il trattamento riduce la carica virale",
                         subquestion_id=subquestion_id or "")]


SPECS = {
    "https://pubmed.ncbi.nlm.nih.gov/33201262/": {
        "source_id": "src_pubmed", "domain": "pubmed.ncbi.nlm.nih.gov",
        "text": T_PUBMED, "title": "Studio clinico COVID",
        "identity_key": f"doi:{DOI}",
        "identity_aliases": [f"doi:{DOI}", "pmid:33201262"],
        "published_at": "2020-11-17",
    },
    "https://pmc.ncbi.nlm.nih.gov/articles/PMC7719368/": {
        "source_id": "src_pmc", "domain": "pmc.ncbi.nlm.nih.gov",
        "text": T_PMC, "title": "Studio clinico COVID - full text",
        "identity_key": f"doi:{DOI}",  # testo DIVERSO ma STESSA identita'
        "identity_aliases": [f"doi:{DOI}", "pmc:PMC7719368"],
        "published_at": "2020-11-17",
    },
    "https://radio.example/storia": {
        "source_id": "src_radio", "domain": "radio.example",
        "text": T_RADIO, "title": "Storia della radio",
        "identity_key": "url:https://radio.example/storia",
        "identity_aliases": ["url:https://radio.example/storia"],
        "published_at": None,
    },
    "https://mirror.example/copia": {
        "source_id": "src_mirror", "domain": "mirror.example",
        "text": T_PUBMED, "title": "Copia identica PubMed",
        "identity_key": "url:https://mirror.example/copia",
        "identity_aliases": ["url:https://mirror.example/copia"],
        "published_at": None,
    },
}


def _build(tmp_path: Path, searcher: ScriptedSearcher, engine, reader):
    settings = Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "cache",
                        cache_enabled=False, usage_emit_every_s=0,
                        max_cycles=1, _env_file=None)
    storage = Storage(settings.db_path)
    bus = EventBus(persist=storage.add_event)
    decisions = DecisionRouter(engine, LLMDecisionEngine(FakeLLMClient([])),
                               storage=storage)
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    templates = {
        "planner.txt": "S\n---SYSTEM---\n{question} {language} {max_subquestions}",
        "reader.txt": "S\n---SYSTEM---\n{question} {subquestion} {external_content}",
        "gap_checker.txt": "S\n---SYSTEM---\n{question} {subquestions_json} {evidence_json}",
        "writer.txt": "S\n---SYSTEM---\n{question} {language} {evidence_json} {subquestions_json}",
        "verifier.txt": "S\n---SYSTEM---\n{question} {claim} {evidence_json}",
    }
    for name, body in templates.items():
        (prompts / name).write_text(body, encoding="utf-8")
    orch = Orchestrator(
        settings, storage, bus, FakeLLMClient([]),
        searcher, ScriptedFetcher(SPECS), decisions,
        Planner(FakeLLMClient([json.dumps(PLAN_JSON)]), prompts_dir=prompts),
        reader,
        GapChecker(FakeLLMClient([json.dumps(GAP_JSON)]), prompts_dir=prompts),
        Writer(FakeLLMClient(_writer_fn), prompts_dir=prompts),
        Verifier(FakeLLMClient(_verifier_fn), prompts_dir=prompts))
    return orch, storage


async def test_identity_duplicate_not_evaluated_not_read_not_counted(tmp_path):
    """Il caso reale PubMed+PMC: contenuto diverso, identita' uguale ->
    source_duplicate (dup_reason=identity) e la copia NON viene ne' valutata
    (decisions), ne' letta (reader), ne' contata come fonte/evidenza."""
    engine = CountingDecisionEngine()
    reader = CountingReader()
    searcher = ScriptedSearcher([["https://pubmed.ncbi.nlm.nih.gov/33201262/",
                                  "https://pmc.ncbi.nlm.nih.gov/articles/PMC7719368/",
                                  "https://radio.example/storia"]])
    orch, storage = _build(tmp_path, searcher, engine, reader)
    run_id = "run_identity"

    outcome = await orch.run("Il remdesivir riduce la carica virale?", "it",
                             Depth.STANDARD, run_id, asyncio.Event())
    assert outcome.status == "completed"
    # 3 doc SUCCESS ma solo 2 fonti nuove: la copia PMC e' duplicato d'identita'
    assert outcome.stats["sources"] == 2
    assert outcome.stats["evidences"] == 2  # pubmed + radio (PMC esclusa)

    dups = [e for e in storage.events_for_run(run_id)
            if e["type"] == "source_duplicate"]
    assert len(dups) == 1
    payload = json.loads(dups[0]["payload_json"])
    assert payload["source_id"] == "src_pmc"
    assert payload["dup_reason"] == "identity"
    assert payload["identity_key"] == f"doi:{DOI}"
    assert payload["content_hash"] != ""   # contenuto DIVERSO: solo l'identita' collide
    assert payload["content_hash"] != content_fingerprint(T_PUBMED)

    # NON valutato: decisions girano solo su pubmed e radio
    assert set(engine.evaluated) == {"src_pubmed", "src_radio"}
    # NON letto: una chiamata reader per documento non duplicato
    assert reader.calls == 2

    # storage: TUTTI e 3 i fetch sono loggati in sources, con identity_key
    rows = {r["id"]: r for r in storage.sources_for_run(run_id)}
    assert set(rows) == {"src_pubmed", "src_pmc", "src_radio"}
    assert rows["src_pmc"]["identity_key"] == f"doi:{DOI}"
    assert rows["src_pubmed"]["identity_key"] == f"doi:{DOI}"
    # lookup persistito per run (es. re-scan difensivo post-riavvio)
    assert storage.identity_seen_any(run_id, [f"doi:{DOI}",
                                              "pmc:PMC7719368"]) is True

    # evidenze: nessuna proviene dalla copia PMC
    evs = storage.evidences_for_run(run_id)
    assert evs and {e["source_id"] for e in evs} == {"src_pubmed", "src_radio"}


async def test_content_duplicate_same_cycle_not_read(tmp_path):
    """Copia identica STESSO ciclo (il bug 'doppioni letti comunque'): prima
    veniva valutata e letta (evidenze duplicate); ora solo source_duplicate."""
    engine = CountingDecisionEngine()
    reader = CountingReader()
    searcher = ScriptedSearcher([["https://pubmed.ncbi.nlm.nih.gov/33201262/",
                                  "https://mirror.example/copia"]])
    orch, storage = _build(tmp_path, searcher, engine, reader)
    run_id = "run_content_dup"

    outcome = await orch.run("Il remdesivir riduce la carica virale?", "it",
                             Depth.STANDARD, run_id, asyncio.Event())
    assert outcome.status == "completed"
    assert outcome.stats["sources"] == 1
    assert outcome.stats["evidences"] == 1  # NON 2: la copia non e' letta
    assert reader.calls == 1                # PRIMA era 2 (bug)
    assert set(engine.evaluated) == {"src_pubmed"}

    dups = [e for e in storage.events_for_run(run_id)
            if e["type"] == "source_duplicate"]
    assert len(dups) == 1
    payload = json.loads(dups[0]["payload_json"])
    assert payload["source_id"] == "src_mirror"
    assert payload["dup_reason"] == "content"
    assert payload["content_hash"] == content_fingerprint(T_PUBMED)
