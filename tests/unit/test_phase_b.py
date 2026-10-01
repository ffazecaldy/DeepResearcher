"""FASE B tests: termination, query similarity, domain cap, report language."""
from __future__ import annotations

import asyncio
import json
import re

from app.agent.query_generator import QueryGenerator
from app.agent.search_state import SearchState, too_similar
from app.config import Depth, Settings
from app.lang_check import looks_italian

# ---------- terminazione: _should_stop ----------


class _Budget:
    def __init__(self, time_left=100.0, pages=0, max_pages=30):
        self._tl = time_left
        self.pages_fetched = pages
        self.max_total_pages = max_pages
        self.max_runtime_seconds = 600

    def time_left(self):
        return self._tl


class _Gap:
    def __init__(self, complete, covered, all_ids):
        self.complete = complete
        self.covered_subquestions = covered
        self.status_by_id = {i: ("coperta" if i in covered else "non_coperta")
                             for i in all_ids}


def _orch():
    import sys
    sys.path.insert(0, ".")
    from app.agent.orchestrator import Orchestrator
    from app.config import Settings

    class _FakeOrch(Orchestrator):
        def __init__(self):
            self.settings = Settings(_env_file=None)

    return _FakeOrch()


def test_stop_complete_coverage():
    o = _orch()
    stop, reason, _ = o._should_stop(
        cycle=2, effective=5, budget=_Budget(), gap_report=_Gap(True, ["a"], ["a"]),
        new_urls=5, new_domains=3, shared_state=SearchState())
    assert stop and reason == "COPERTURA_COMPLETA"


def test_stop_max_cycles_is_default_and_not_early():
    o = _orch()
    stop, reason, _ = o._should_stop(
        cycle=1, effective=5, budget=_Budget(), gap_report=_Gap(False, [], ["a"]),
        new_urls=5, new_domains=3, shared_state=SearchState())
    assert not stop  # ciclo 1 con novità: si continua
    # nessuna delle cause anticipate: il loop finirà per MAX_CICLI
    assert reason == ""


def test_stop_time_limit():
    o = _orch()
    stop, reason, _ = o._should_stop(
        cycle=1, effective=5, budget=_Budget(time_left=0.0),
        gap_report=_Gap(False, [], ["a"]), new_urls=5, new_domains=3,
        shared_state=SearchState())
    assert stop and reason == "LIMITE_TEMPO"


def test_stop_no_novelty_after_two_cycles():
    o = _orch()
    shared = SearchState()
    for _ in range(2):
        stop, reason, _ = o._should_stop(
            cycle=2, effective=5, budget=_Budget(), gap_report=_Gap(False, [], ["a"]),
            new_urls=0, new_domains=0, shared_state=shared)
    assert stop and reason == "NESSUNA_NOVITA"


def test_stop_unsolvable_after_two_attempts_no_loop():
    o = _orch()
    shared = SearchState()
    shared.attempts_by_sq = {"sq_a": 2}
    stop, reason, detail = o._should_stop(
        cycle=3, effective=5, budget=_Budget(), gap_report=_Gap(False, [], ["sq_a"]),
        new_urls=4, new_domains=2, shared_state=shared)
    assert stop and reason == "SOTTODOMANDE_NON_RISOLVIBILI"
    assert "sq_a" in detail


# ---------- similarita' query ----------


def test_too_similar_catches_rephrasing():
    executed = ["best AI coding assistants 2025 comparison"]
    assert too_similar("best AI coding assistants 2025 compared", executed, 0.6)
    assert not too_similar("normativa europea intelligenza artificiale atto UE",
                           executed, 0.6)


def test_query_generator_filters_similar_and_executed(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "query_generator.txt").write_text(
        "S\n---SYSTEM---\n{question} {subquestions_json} {executed_queries_json}\n",
        encoding="utf-8")

    from app.models import SubQuestion
    from tests.fakes import FakeLLMClient

    gen = QueryGenerator(FakeLLMClient([json.dumps({
        "queries": [
            {"subquestion_id": "sq1", "text": "best AI coding assistants 2025 comparison"},
            {"subquestion_id": "sq1", "text": "normativa europea AI act requisiti"},
            {"subquestion_id": "sq1", "text": "critiche limiti AI coding tools"},
        ]})]), prompts_dir=prompts)
    state = SearchState()
    state.see_query("best AI coding assistants 2025 comparison")

    import sys
    sys.path.insert(0, ".")
    sqs = [SubQuestion(subquestion_id="sq1", text="harness?", idx=1)]
    out = asyncio.run(gen.generate("q", sqs, state, 2))

    texts = [q.text for q in out]
    assert "best AI coding assistants 2025 comparison" not in texts  # già eseguita
    assert len(out) == 2  # le altre 2 passano il filtro


# ---------- domain cap ----------


def test_domain_cap_in_searcher(tmp_path):
    import sys

    sys.path.insert(0, ".")
    from app.agent.searcher import Searcher
    from app.config import Settings
    from app.limits import DiskCache
    from app.models import GeneratedQuery, SearchResultItem

    class _C:
        name = "fake"

        async def search(self, query, max_results):
            return [SearchResultItem(query_id="", url=f"https://stesso.it/p{i}",
                                     title=f"p{i}") for i in range(5)]

        async def aclose(self):
            pass

    settings = Settings(db_path=tmp_path / "t.db", cache_dir=tmp_path / "c",
                        cache_enabled=False, max_pages_per_domain=2,
                        _env_file=None)
    s = Searcher(_C(), DiskCache(tmp_path / "c", False), settings)
    out = asyncio.run(s.run_queries(
        [GeneratedQuery(query_id="q1", subquestion_id="sq", text="x")], 5))
    assert len(out) == 2  # cap per dominio


# ---------- lingua report ----------


def test_italian_detection():
    assert looks_italian("Il mercato del lavoro cambia con l'intelligenza "
                         "artificiale. Le fonti indicano che la normativa "
                         "europea sarà applicata per tutte le imprese del "
                         "settore, secondo i dati raccolti nelle varie "
                         "indagini sulla produttità e sull'occupazione.")
    assert not looks_italian("The labor market is changing with artificial "
                             "intelligence. Sources indicate the new regulation "
                             "will apply to all companies within the union, "
                             "although several analysts noted that the rules "
                             "would be phased in over time and enforcement "
                             "would vary between member states and sectors.")


def test_writer_strips_double_h1(tmp_path):
    import sys

    sys.path.insert(0, ".")
    from app.agent.writer import Writer
    from app.models import Evidence
    from tests.fakes import FakeLLMClient

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "writer.txt").write_text(
        "SYS\n---SYSTEM---\n{question} {language} {evidence_json} {subquestions_json}\n",
        encoding="utf-8")
    fake = FakeLLMClient([json.dumps({
        "title": "Titolo",
        "markdown": "# Titolo\n\nTesto italiano.",
        "claims": [{"text": "Testo italiano.", "evidence_ids": ["ev1"]}],
    })])
    w = Writer(fake, prompts_dir=prompts)
    ev = Evidence(evidence_id="ev1", source_id="s", chunk_id="c",
                  quote="q", claim="c", run_id="r")
    draft = asyncio.run(w.write("domanda", "it", [], [ev]))
    assert not re.match(r"^\s*#\s+", draft.markdown)  # niente H1 nel body
    assert draft.markdown == "Testo italiano."
