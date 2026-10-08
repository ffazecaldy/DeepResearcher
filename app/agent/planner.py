"""Planner: question -> Plan (subquestions + per-subquestion search queries)."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.models import GeneratedQuery, Plan, SubQuestion

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

_JACCARD_THRESHOLD = 0.8
_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)


class PlannerError(RuntimeError):
    """The planner could not obtain a valid plan JSON after the repair retry."""


def _load_template(name: str) -> tuple[str, str]:
    raw = (_PROMPTS_DIR / name).read_text(encoding="utf-8")
    if "---SYSTEM---" not in raw:
        raise PlannerError(f"prompt file {name} missing ---SYSTEM--- marker")
    system, user = raw.split("---SYSTEM---", 1)
    return system.strip(), user.strip()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


def _extract_json_object(text: str) -> str:
    """First balanced {...} block (brace-counting, string-aware)."""
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


def _clamp_nonneg_int(value, fallback: int, maximum: int) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return fallback
    if iv < 0:
        return fallback
    return min(iv, maximum)


def _dedupe_queries(items: list[GeneratedQuery]) -> list[GeneratedQuery]:
    """Drop a query when its token Jaccard vs a previous one exceeds 0.8."""
    kept: list[GeneratedQuery] = []
    kept_tokens: list[set[str]] = []
    for q in items:
        toks = _tokens(q.text)
        if any(_jaccard(toks, prev) > _JACCARD_THRESHOLD for prev in kept_tokens):
            log.debug("planner: dropped near-duplicate query %r", q.text)
            continue
        kept.append(q)
        kept_tokens.append(toks)
    return kept


class Planner:
    def __init__(self, llm, prompts_dir: Path | None = None):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR

    def _load(self) -> tuple[str, str]:
        raw = (self._prompts_dir / "planner.txt").read_text(encoding="utf-8")
        if "---SYSTEM---" not in raw:
            raise PlannerError("planner.txt missing ---SYSTEM--- marker")
        system, user = raw.split("---SYSTEM---", 1)
        return system.strip(), user.strip()

    async def make_plan(self, question: str, language: str,
                        max_subquestions: int = 5) -> Plan:
        system_tpl, user_tpl = self._load()
        user = user_tpl.format(question=question, language=language,
                               max_subquestions=max_subquestions)

        raw = await self._llm.complete(system_tpl, user, force_json=True)
        data = None
        try:
            data = json.loads(_extract_json_object(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("planner: invalid JSON (%s), one repair retry", exc)
            repair = (
                "La risposta precedente non era JSON valido e non pu\u00f2 essere "
                "interpretata. Rispondi SOLO con un oggetto JSON valido, senza "
                "testo extra, correggendo l'errore:\n" + raw[-4000:]
            )
            raw2 = await self._llm.complete(system_tpl, repair,
                                            force_json=True)
            try:
                data = json.loads(_extract_json_object(raw2))
            except (ValueError, json.JSONDecodeError) as exc2:
                raise PlannerError(f"planner returned invalid JSON twice: {exc2}") from exc2

        if not isinstance(data, dict):
            raise PlannerError("plan JSON is not an object")

        subqs = data.get("subquestions")
        queries = data.get("queries")
        if not isinstance(subqs, list) or not subqs:
            raise PlannerError("plan JSON missing non-empty 'subquestions'")
        if not isinstance(queries, list) or not queries:
            raise PlannerError("plan JSON missing non-empty 'queries'")

        # deterministic ids: model ids are IGNORED for subquestions, but each
        # query's subquestion_id is resolved from the MODEL's own id space
        # BEFORE rewriting (P1-5: a valid model id must not fall back to sq1).
        sub_models: list[SubQuestion] = []
        id_by_text: dict[str, str] = {}      # normalized text -> normalized id
        model_id_to_norm: dict[str, str] = {}  # model id -> normalized id
        for i, s in enumerate(subqs):
            if not isinstance(s, dict) or not str(s.get("text", "")).strip():
                continue
            text = str(s["text"]).strip()
            model_raw = str(s.get("subquestion_id", f"sq{i + 1}")).strip()
            sid = f"sq{i + 1}"
            id_by_text[text.lower()] = sid
            model_id_to_norm[model_raw] = sid
            model_id_to_norm[model_raw.lower()] = sid
            sub_models.append(SubQuestion(subquestion_id=sid, text=text, idx=i + 1))
        if not sub_models:
            raise PlannerError("plan has no valid subquestions")

        gen: list[GeneratedQuery] = []
        n = 0
        for q in queries:
            if not isinstance(q, dict):
                continue
            text = str(q.get("text", "")).strip()
            if not text:
                continue
            sq_raw = str(q.get("subquestion_id", "")).strip()
            # 1) model id (exact or case-insensitive) -> normalized id
            sid = (model_id_to_norm.get(sq_raw)
                   or model_id_to_norm.get(sq_raw.lower()) or "")
            # 2) reference by exact subquestion text
            if sid == "":
                sid = id_by_text.get(sq_raw.lower(), "")
            # 3) positional hint, else first (logged, never silent)
            if sid == "":
                order = q.get("_order")
                if isinstance(order, int) and 1 <= order <= len(sub_models):
                    sid = sub_models[order - 1].subquestion_id
                else:
                    log.warning("planner: query %r references unknown "
                                "subquestion_id %r -> sq1", text[:40], sq_raw)
                    sid = sub_models[0].subquestion_id
            n += 1
            gen.append(GeneratedQuery(query_id=f"q{n}", subquestion_id=sid,
                                      text=text))
        if not gen:
            raise PlannerError("plan has no valid queries")

        gen = _dedupe_queries(gen)
        if not gen:
            raise PlannerError("all queries were near-duplicates")

        # contiguous ids AFTER dedup: no numbering gaps
        for i, q in enumerate(gen, start=1):
            q.query_id = f"q{i}"

        title = str(data.get("title", "")).strip() or question.strip()
        return Plan(title=title, language=language,
                    subquestions=sub_models, queries=gen)
