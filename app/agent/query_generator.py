"""QueryGenerator: diversified queries for still-open sub-questions (cycle >= 2)."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.agent.search_state import SearchState, too_similar
from app.models import GeneratedQuery, NewQuery, SubQuestion

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
_SIMILARITY_THRESHOLD = 0.6  # Jaccard over token sets
_MAX_REGEN_ATTEMPTS = 2


class QueryGeneratorError(RuntimeError):
    """The query generator could not obtain valid JSON after retries."""


def _extract_json_object(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
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


class QueryGenerator:
    """Generates diversified queries for open sub-questions, with similarity
    filtering against the shared SearchState."""

    def __init__(self, llm, prompts_dir: Path | None = None,
                 similarity_threshold: float = _SIMILARITY_THRESHOLD):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR
        self._threshold = similarity_threshold

    async def generate(self, question: str, open_subquestions: list[SubQuestion],
                       state: SearchState, cycle: int, *,
                       per_sq_limit: int = 3) -> list[GeneratedQuery]:
        """Returns NEW queries, similarity-filtered; [] when nothing new survives."""
        if not open_subquestions:
            return []
        raw_tpl = (self._prompts_dir / "query_generator.txt").read_text("utf-8")
        if "---SYSTEM---" not in raw_tpl:
            raise QueryGeneratorError("query_generator.txt missing ---SYSTEM---")
        system_tpl, user_tpl = raw_tpl.split("---SYSTEM---", 1)
        system_tpl, user_tpl = system_tpl.strip(), user_tpl.strip()

        sq_json = json.dumps(
            [{"subquestion_id": s.subquestion_id, "text": s.text}
             for s in open_subquestions], ensure_ascii=False)
        done_json = json.dumps(state.executed_queries[-30:], ensure_ascii=False)
        user = user_tpl.format(question=question, subquestions_json=sq_json,
                               executed_queries_json=done_json)

        accepted: list[GeneratedQuery] = []
        prompt = user
        for _attempt in range(_MAX_REGEN_ATTEMPTS + 1):
            raw = await self._llm.complete(system_tpl, prompt, force_json=True)
            try:
                data = json.loads(_extract_json_object(raw))
            except (ValueError, json.JSONDecodeError) as exc:
                log.warning("query_generator: invalid JSON (%s), retrying", exc)
                prompt = (user + "\n\nLa risposta precedente non era JSON valido. "
                          "Rispondi SOLO con JSON valido.")
                continue
            if not isinstance(data, dict):
                continue
            open_ids = {s.subquestion_id for s in open_subquestions}
            per_sq: dict[str, int] = {}
            for q in data.get("queries", []) or []:
                if not isinstance(q, dict):
                    continue
                text = str(q.get("text", "")).strip()
                sid = str(q.get("subquestion_id", "")).strip()
                if not text or sid not in open_ids:
                    continue
                if per_sq.get(sid, 0) >= per_sq_limit:
                    continue
                if state.already_ran(text):
                    continue
                if too_similar(text, state.executed_queries, self._threshold):
                    log.info("query_generator: scartata per similarita': %r", text)
                    continue
                per_sq[sid] = per_sq.get(sid, 0) + 1
                accepted.append(GeneratedQuery(
                    query_id=f"{state.executed_queries and ''}g{cycle}_{len(accepted) + 1}",
                    subquestion_id=sid, text=text, cycle=cycle))
            if accepted:
                break
            prompt = (user + "\n\nTutte le query proposte erano troppo simili a "
                      "quelle gia' eseguite. Genera query RADICALMENTE diverse: "
                      "nuovi termini, altre lingue, angolazioni differenti.")
        return accepted
