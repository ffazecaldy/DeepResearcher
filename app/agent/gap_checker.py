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


def compute_coverage(subquestions: list[SubQuestion],
                     evidences: list[Evidence],
                     domain_of: dict[str, str] | None = None) -> tuple[bool, dict[str, str]]:
    """Code-decided coverage per sub-question.

    Returns (all_covered, status_by_id) with status in
    {"coperta", "parziale", "non_coperta"}. Independence = distinct domains:
    two pages of the same domain count as ONE source.
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
