"""GapChecker: subquestions + evidences -> GapReport (sanitized).

Coverage is DECIDED IN CODE, not by the LLM: a sub-question is "coperta" only
with >=2 notes from INDEPENDENT sources (different domains); 1 source is
"parziale", 0 is "non_coperta". The LLM proposes, the code decides.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.models import Evidence, GapReport, NewQuery, SubQuestion

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
_TRUNC = 160
_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)

MIN_SOURCES_COVERED = 2  # independent (distinct-domain) notes for "coperta"

# tipi di fonte considerati PRIMARI (allineati alle opzioni della decisione
# source_type dell'orchestrator: official/paper, piu' sinonimi tolleranti)
PRIMARY_SOURCE_TYPES = {"official", "paper", "primaria", "primary",
                        "studio", "ufficiale"}

# token troppo generici per collegare una contraddizione a una sotto-domanda
_STOP_TOKENS = {"come", "dove", "quando", "quale", "quali", "della", "delle",
                "dello", "nella", "nelle", "sulla", "sullo", "perche",
                "essere", "hanno", "anche", "senza", "secondo", "studio",
                "fonte", "affermazione"}


class GapCheckError(RuntimeError):
    """The gap check could not obtain a valid JSON verdict."""


def load_template(prompts_dir: Path, name: str) -> tuple[str, str]:
    raw = (prompts_dir / name).read_text(encoding="utf-8")
    if "---SYSTEM---" not in raw:
        raise GapCheckError(f"prompt file {name} missing ---SYSTEM--- marker")
    system, user = raw.split("---SYSTEM---", 1)
    return system.strip(), user.strip()


# backwards-compatible alias
_load_template = load_template


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


def _extract_json_object(text: str) -> str:
    t = _strip_json_fence(text)
    start = t.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(t[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    raise ValueError("unbalanced JSON object")


def _trunc(s: str, n: int = _TRUNC) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _norm_key(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _significant_tokens(text: str) -> set[str]:
    """Token sufficientemente specifici per matching contraddizione<->sotto-q."""
    toks = {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 4}
    return toks - _STOP_TOKENS


def _contradiction_hits(sq: SubQuestion, contradictions: list[str]) -> list[str]:
    """Contraddizioni che riguardano una sotto-domanda.

    Euristica documentata e deterministica: una contraddizione (stringa libera
    del GapChecker LLM) e' attribuita alla sotto-domanda se la cita per id OPPURE
    condivide >= 2 token significativi con il suo testo.
    """
    hits: list[str] = []
    sq_toks = _significant_tokens(sq.text)
    for c in contradictions or []:
        cid = str(c)
        if sq.subquestion_id and sq.subquestion_id in cid:
            hits.append(cid)
            continue
        if len(sq_toks & _significant_tokens(cid)) >= 2:
            hits.append(cid)
    return hits


def build_gap_matrix(subquestions: list[SubQuestion],
                     evidences: list[Evidence],
                     domain_of: dict[str, str] | None = None,
                     contradictions: list[str] | None = None) -> list[dict]:
    """Matrice PURA delle lacune: una riga dict per sotto-domanda.

    Ritorna una lista di dict con chiavi:
    - ``subquestion_id``: id della sotto-domanda;
    - ``text``: testo della sotto-domanda;
    - ``evidence_count``: numero di evidenze associate (anche stesso dominio);
    - ``distinct_domains``: domini INDIPENDENTI distinti (regola >= 2 = coperta);
    - ``status``: "coperta" | "parziale" | "non_coperta" (stessa regola di
      compute_coverage; le contraddizioni NON cambiano lo status, vivono in
      problems/next_step);
    - ``problems``: elenco concreto delle lacune, tra:
      "nessuna fonte", "una sola fonte indipendente",
      "nessuna fonte primaria", "contraddizione aperta".
      "nessuna fonte primaria" e' segnalata SOLO quando almeno una evidenza ha
      un source_type classificato e nessuna e' primaria (source_type ignoto ->
      il problema non si puo' stabilire e NON e' inventato);
    - ``next_step``: azione operativa successiva, tra:
      "cerca smentite o aggiornamenti" (priorita' alle contraddizioni aperte),
      "cerca una prima fonte indipendente (istituzionale o primaria)",
      "cerca conferma su una seconda fonte indipendente",
      "cerca fonte primaria o studio originale", "nessuna azione richiesta".
    """
    domain_of = domain_of or {}
    contradictions = [str(c) for c in (contradictions or []) if c]
    matrix: list[dict] = []
    for s in subquestions:
        evs = [e for e in evidences if e.subquestion_id == s.subquestion_id]
        domains = {domain_of.get(e.source_id, e.source_id)
                   for e in evs if e.source_id}
        n, nd = len(evs), len(domains)
        status = ("coperta" if nd >= MIN_SOURCES_COVERED
                  else "parziale" if nd == 1 else "non_coperta")

        problems: list[str] = []
        if n == 0:
            problems.append("nessuna fonte")
        elif nd == 1:
            problems.append("una sola fonte indipendente")
        classified = [e for e in evs if e.source_type]
        if n > 0 and classified and \
                not any(e.source_type.lower() in PRIMARY_SOURCE_TYPES
                        for e in classified):
            problems.append("nessuna fonte primaria")
        hits = _contradiction_hits(s, contradictions)
        if hits:
            problems.append("contraddizione aperta")

        if "contraddizione aperta" in problems:
            next_step = "cerca smentite o aggiornamenti"
        elif "nessuna fonte" in problems:
            next_step = "cerca una prima fonte indipendente (istituzionale o primaria)"
        elif "una sola fonte indipendente" in problems:
            next_step = "cerca conferma su una seconda fonte indipendente"
        elif "nessuna fonte primaria" in problems:
            next_step = "cerca fonte primaria o studio originale"
        else:
            next_step = "nessuna azione richiesta"

        matrix.append({
            "subquestion_id": s.subquestion_id,
            "text": _trunc(s.text),
            "evidence_count": n,
            "distinct_domains": nd,
            "status": status,
            "problems": problems,
            "next_step": next_step,
        })
    return matrix


def compute_coverage(subquestions: list[SubQuestion],
                     evidences: list[Evidence],
                     domain_of: dict[str, str] | None = None) -> tuple[bool, dict[str, str]]:
    """Code-decided coverage per sub-question.

    Returns (all_covered, status_by_id) with status in
    {"coperta", "parziale", "non_coperta"}. Independence = distinct domains:
    two pages of the same domain count as ONE source.

    The same deterministic rule feeds :func:`build_gap_matrix`, whose row list
    (one dict per sub-question with evidence_count, distinct_domains, problems
    and next_step) is logged at INFO for diagnosability. The return value and
    signature are unchanged: the matrix enriches the LOG, not the contract.
    """
    domain_of = domain_of or {}
    by_sq: dict[str, set[str]] = {s.subquestion_id: set() for s in subquestions}
    for e in evidences:
        sid = e.subquestion_id
        if sid in by_sq and e.source_id:
            by_sq[sid].add(domain_of.get(e.source_id, e.source_id))
    status: dict[str, str] = {}
    for s in subquestions:
        n = len(by_sq[s.subquestion_id])
        status[s.subquestion_id] = ("coperta" if n >= MIN_SOURCES_COVERED
                                    else "parziale" if n == 1 else "non_coperta")
    # matrice lacune nel log (contraddizioni non note qui -> lista vuota;
    # GapChecker.check logga la matrice completa con le contraddizioni reali)
    log.info("gap_checker: matrice lacune %s",
             json.dumps(build_gap_matrix(subquestions, evidences, domain_of, []),
                        ensure_ascii=False))
    return all(v == "coperta" for v in status.values()), status


def _sanitize_llm(data: dict, valid_ids: set[str]) -> tuple[list[NewQuery], list[str]]:
    """Sanitize LLM-proposed new queries and contradictions (best effort)."""
    contradictions = [str(x) for x in data.get("contradictions", []) or [] if x]
    new_queries: list[NewQuery] = []
    seen: set[tuple[str, str]] = set()
    for q in data.get("new_queries", []) or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("text", "")).strip()
        sid = str(q.get("subquestion_id", "")).strip()
        if not text or sid not in valid_ids:
            continue
        key = (sid, _norm_key(text))
        if key in seen:
            continue
        seen.add(key)
        new_queries.append(NewQuery(subquestion_id=sid, text=text))
    return new_queries, contradictions


class GapChecker:
    def __init__(self, llm, prompts_dir: Path | None = None):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR

    async def check(self, question: str, subquestions: list[SubQuestion],
                    evidences: list[Evidence],
                    domain_of: dict[str, str] | None = None) -> GapReport:
        """Ask the LLM for analysis, then DECIDE coverage in code.

        A malformed LLM verdict NEVER terminates the run: coverage falls back
        to the deterministic rule and the run continues.
        """
        system_tpl, user_tpl = load_template(self._prompts_dir, "gap_checker.txt")

        sq_json = json.dumps(
            [{"subquestion_id": s.subquestion_id, "text": _trunc(s.text)}
             for s in subquestions],
            ensure_ascii=False,
        )
        ev_json = json.dumps(
            [{"subquestion_id": e.subquestion_id or "", "claim": _trunc(e.claim),
              "quote": _trunc(e.quote)} for e in evidences],
            ensure_ascii=False,
        )
        user = user_tpl.format(question=question, subquestions_json=sq_json,
                               evidence_json=ev_json)

        valid_ids = {s.subquestion_id for s in subquestions}
        new_queries: list[NewQuery] = []
        contradictions: list[str] = []
        try:
            raw = await self._llm.complete(system_tpl, user, force_json=True)
            data = json.loads(_extract_json_object(raw))
            if not isinstance(data, dict):
                raise ValueError("JSON is not an object")
            new_queries, contradictions = _sanitize_llm(data, valid_ids)
        except Exception as exc:
            log.warning("gap_checker: LLM verdict unusable (%s); coverage is "
                        "decided in code and the run continues", exc)

        # the CODE decides coverage (LLM proposes, code disposes).
        # P1-7: unresolved contradictions keep the research open — a covered
        # subquestion with an open contradiction is not a finished answer.
        complete, status = compute_coverage(subquestions, evidences, domain_of)
        # matrice COMPLETA (con le contraddizioni reali) nel log: per ogni
        # sotto-domanda dice cosa manca e quale e' il prossimo passo utile
        log.info("gap_checker: matrice lacune completa %s",
                 json.dumps(build_gap_matrix(subquestions, evidences, domain_of,
                                             contradictions),
                            ensure_ascii=False))
        if complete and contradictions:
            log.info("gap_checker: copertura completa ma %d contraddizioni "
                     "aperte -> la ricerca continua", len(contradictions))
            complete = False
        covered = [s.subquestion_id for s in subquestions
                   if status[s.subquestion_id] == "coperta"]
        partial = [s.subquestion_id for s in subquestions
                   if status[s.subquestion_id] == "parziale"]
        missing = [s.subquestion_id for s in subquestions
                   if status[s.subquestion_id] == "non_coperta"]

        return GapReport(
            complete=complete,
            covered_subquestions=covered,
            missing_subquestions=missing,
            contradictions=contradictions,
            new_queries=new_queries,
            partial_subquestions=partial,
            status_by_id=status,
        )
