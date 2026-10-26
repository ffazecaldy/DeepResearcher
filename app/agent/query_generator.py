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

# Cambio strategia: soglie per decidere che le prossime ricerche sugli stessi
# binari restituirebbero soprattutto documenti GIA' LETTI. Deterministico,
# calcolato sullo stato passato al prompt (URL visti, domini, query eseguite).
_STRATEGY_SHIFT_MIN_URLS = 5       # sotto questa soglia lo stato non e' signif.
_STRATEGY_SHIFT_TOP_SHARE = 0.6    # >=60% degli URL visti su UN solo dominio
_STRATEGY_SHIFT_MIN_QUERIES = 8    # tante query gia' eseguite -> corpus saturo


def needs_strategy_shift(state: SearchState) -> bool:
    """True quando lo stato indica saturazione: nuove query "piu' dello stesso"
    tornerebbero soprattutto documenti gia' letti/valutati.

    Segnali (basta uno):
    - molti URL visti concentrati su pochissimi domini (top-share elevata);
    - tante query gia' eseguite rispetto alla produzione di novita';
    - diversi URL letti ma quasi nessuna evidenza estratta.
    """
    n_seen = len(state.seen_urls)
    if n_seen == 0:
        return False
    if len(state.executed_queries) >= _STRATEGY_SHIFT_MIN_QUERIES:
        return True
    if n_seen >= _STRATEGY_SHIFT_MIN_URLS and state.evidence_count == 0:
        return True
    top = max(state.domain_count.values(), default=0)
    return (n_seen >= _STRATEGY_SHIFT_MIN_URLS
            and n_seen > 0 and top / n_seen >= _STRATEGY_SHIFT_TOP_SHARE)


def _strategy_shift_block(state: SearchState) -> str:
    """Estensione del prompt: cambio strategia esplicito + documenti utili letti."""
    n_seen = len(state.seen_urls)
    top_domain = (max(state.domain_count.items(), key=lambda kv: kv[1])[0]
                  if state.domain_count else "sconosciuto")
    docs: list[str] = []
    for meta in list(state.read_sources.values())[-3:]:
        title = str(meta.get("title") or meta.get("url") or "").strip()
        dom = str(meta.get("domain") or "").strip()
        docs.append(f"- {title} ({dom})" if dom else f"- {title}")
    doc_lines = "\n".join(docs) if docs else "- (nessun documento utile memorizzato)"
    return (
        "\n\nCAMBIO STRATEGIA RICHIESTO: le ricerche precedenti hanno "
        "restituito soprattutto documenti gia' letti o valutati "
        f"({n_seen} URL visti; dominio dominante: {top_domain}). Le nuove "
        "query DEVONO cambiare strategia in modo ESPLICITO:\n"
        "1. termini di ricerca diversi (sinonimi, altro lessico, altra "
        "angolazione — non riphrasing di quelle gia' eseguite);\n"
        "2. tipo di fonte diverso (es. da notizie a documentazione "
        "istituzionale/accademica, o viceversa);\n"
        "3. lingua diversa: almeno una query in una lingua non ancora usata;\n"
        "4. ricerca bibliografica: bibliografie, revisioni, \"related work\", "
        "liste di riferimenti;\n"
        "5. riferimenti dei documenti utili gia' letti: parti dai documenti "
        "elencati sotto e cerca i documenti che LORO citano (non rileggerli).\n"
        "Documenti gia' letti (usa i loro riferimenti):\n"
        f"{doc_lines}")


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
        # cambio strategia: se lo stato dice che la ricerca tornerebbe
        # soprattutto documenti gia' letti, estendi il prompt PRIMA della call
        if needs_strategy_shift(state):
            log.info("query_generator: stato saturo (%d URL visti, %d query) "
                     "-> cambio strategia richiesto nel prompt",
                     len(state.seen_urls), len(state.executed_queries))
            user = user + _strategy_shift_block(state)

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
