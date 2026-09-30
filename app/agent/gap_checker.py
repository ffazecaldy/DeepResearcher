"""GapChecker: subquestions + evidences -> GapReport (sanitized)."""
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


class GapCheckError(RuntimeError):
    """The gap check could not obtain a valid JSON verdict."""


def _load_template(prompts_dir: Path, name: str) -> tuple[str, str]:
    raw = (prompts_dir / name).read_text(encoding="utf-8")
    if "---SYSTEM---" not in raw:
        raise GapCheckError(f"prompt file {name} missing ---SYSTEM--- marker")
    system, user = raw.split("---SYSTEM---", 1)
    return system.strip(), user.strip()


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
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _norm_key(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _sanitize(data: dict, valid_sq_ids: set[str]) -> GapReport:
    covered = [str(x) for x in data.get("covered_subquestions", []) or []
               if str(x) in valid_sq_ids]
    missing = [str(x) for x in data.get("missing_subquestions", []) or []
               if str(x) in valid_sq_ids]
    contradictions = [str(x) for x in data.get("contradictions", []) or []]

    new_queries: list[NewQuery] = []
    seen: set[tuple[str, str]] = set()
    for q in data.get("new_queries", []) or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("text", "")).strip()
        sid = str(q.get("subquestion_id", "")).strip()
        if not text or sid not in valid_sq_ids:
            continue
        key = (sid, _norm_key(text))
        if key in seen:
            continue
        seen.add(key)
        new_queries.append(NewQuery(subquestion_id=sid, text=text))

    return GapReport(
        complete=bool(data.get("complete", False)),
        covered_subquestions=covered,
        missing_subquestions=missing,
        contradictions=[c for c in contradictions if c],
        new_queries=new_queries,
    )


class GapChecker:
    def __init__(self, llm, prompts_dir: Path | None = None):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR

    async def check(self, question: str, subquestions: list[SubQuestion],
                    evidences: list[Evidence]) -> GapReport:
        system_tpl, user_tpl = _load_template(self._prompts_dir, "gap_checker.txt")

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

        raw = await self._llm.complete(system_tpl, user, force_json=True)
        try:
            data = json.loads(_extract_json_object(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            raise GapCheckError(f"gap_checker returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise GapCheckError("gap_checker JSON is not an object")

        valid_ids = {s.subquestion_id for s in subquestions}
        return _sanitize(data, valid_ids)
