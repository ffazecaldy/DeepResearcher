"""Writer: builds the report strictly from the evidence store."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.models import DraftReport, Evidence, ReportClaim, SubQuestion

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


class WriterError(RuntimeError):
    """The writer could not obtain a valid report JSON after the repair retry."""


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


_WRITER_MAX_EVIDENCES = 30  # prompt cap: too many ids -> truncated/garbled JSON


def _trim_evidences(evidences: list[Evidence],
                    cap: int = _WRITER_MAX_EVIDENCES) -> list[Evidence]:
    """Evenly sample evidences so every source/subquestion stays represented."""
    if len(evidences) <= cap:
        return evidences
    step = len(evidences) / cap
    return [evidences[int(i * step)] for i in range(cap)]


class Writer:
    def __init__(self, llm, prompts_dir: Path | None = None):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR

    def _load(self) -> tuple[str, str]:
        raw = (self._prompts_dir / "writer.txt").read_text(encoding="utf-8")
        if "---SYSTEM---" not in raw:
            raise WriterError("writer.txt missing ---SYSTEM--- marker")
        system, user = raw.split("---SYSTEM---", 1)
        return system.strip(), user.strip()

    async def write(self, question: str, language: str,
                    subquestions: list[SubQuestion], evidences: list[Evidence],
                    *, limit_note: str = "") -> DraftReport:
        if not evidences:
            raise WriterError("cannot write a report with zero evidences")
        prompt_evidences = _trim_evidences(evidences)
        valid_ids = {e.evidence_id for e in evidences}  # ALL ids remain valid targets
        system_tpl, user_tpl = self._load()
        ev_json = json.dumps(
            [{"evidence_id": e.evidence_id, "source_id": e.source_id,
              "claim": e.claim, "quote": e.quote[:200]} for e in prompt_evidences],
            ensure_ascii=False)
        sq_json = json.dumps(
            [{"subquestion_id": s.subquestion_id, "text": s.text}
             for s in subquestions], ensure_ascii=False)
        user = user_tpl.format(question=question, language=language,
                               evidence_json=ev_json, subquestions_json=sq_json)
        # BUG3: language rule at the START and the END of the user prompt
        lang_rule = (f"REGOLA LINGUA OBBLIGATORIA: scrivi l'INTERO report "
                     f"(titolo, testo, sintesi, incertezze) esclusivamente in "
                     f"italiano ({language}), anche se le fonti sono in inglese "
                     f"o in un'altra lingua. Traduci concetti e citazioni; "
                     f"mantieni in lingua originale SOLO nomi propri, sigle e "
                     f"termini tecnici senza equivalente comune.")
        user = f"{lang_rule}\n\n{user}\n\n{lang_rule}"

        data = await self._ask(system_tpl, user)
        claims = self._extract_claims(data, valid_ids)
        if not claims:
            # one explicit rescue: the model must anchor claims to the given ids
            ids_hint = ", ".join(sorted(valid_ids)[:30])
            repair = (user + "\n\nATTENZIONE: la risposta precedente non conteneva "
                      "claim collegati alle evidenze. Riscrivi il report usando "
                      "SOLO questi evidence_id: [" + ids_hint + "]")
            data = await self._ask(system_tpl, repair)
            claims = self._extract_claims(data, valid_ids)
        if not claims:
            raise WriterError("writer produced no claim backed by known evidence")
        for i, c in enumerate(claims, start=1):
            c.citation_no = i

        markdown = str(data.get("markdown", "")).strip()
        # single H1: the writer must NOT emit its own top title (added once later)
        markdown = re.sub(r"^\s*#\s+[^\n]+\n+", "", markdown)
        title = str(data.get("title", "")).strip() or question.strip()
        if limit_note:
            markdown += (f"\n\n> Nota: {limit_note}\n")
        return DraftReport(title=title, markdown=markdown, claims=claims)

    async def _ask(self, system_tpl: str, user: str) -> dict:
        raw = await self._llm.complete(system_tpl, user, force_json=True)
        try:
            data = json.loads(_extract_json_object(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("writer: invalid JSON (%s), one repair retry", exc)
            repair = ("La risposta precedente non era JSON valido. Rispondi SOLO "
                      "con un oggetto JSON valido correggendo l'errore:\n"
                      + raw[-4000:])
            raw2 = await self._llm.complete(system_tpl, repair, force_json=True)
            try:
                data = json.loads(_extract_json_object(raw2))
            except (ValueError, json.JSONDecodeError) as exc2:
                raise WriterError(f"writer returned invalid JSON twice: {exc2}") from exc2
        if not isinstance(data, dict):
            raise WriterError("writer JSON is not an object")
        return data

    def _extract_claims(self, data: dict, valid_ids: set[str]) -> list[ReportClaim]:
        claims: list[ReportClaim] = []
        for c in data.get("claims", []) or []:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text", "")).strip()
            ev_ids = [str(x) for x in (c.get("evidence_ids", []) or [])
                      if str(x) in valid_ids]
            if not text or not ev_ids:
                continue
            claims.append(ReportClaim(text=text, citation_no=0,
                                      evidence_ids=ev_ids))
        return claims
