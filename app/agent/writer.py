"""Writer: builds the report strictly from the evidence store."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.agent.citations import sanitize_brackets
from app.models import DraftReport, Evidence, ReportClaim, SubQuestion

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

# marker di citazione validi: singolo [n] o lista [a, b] (solo numeri interi)
_MARKER = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")


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
    """P1-11: evidence pack balanced by source and subquestion.

    Round-robin across (subquestion, source) groups so every aspect of the
    question and every source stays represented; within a group, higher
    quality_score first. Deterministic; falls back to even sampling when
    there are no groups at all.
    """
    if len(evidences) <= cap:
        return evidences
    groups: dict[tuple[str, str], list[Evidence]] = {}
    for e in evidences:
        groups.setdefault((e.subquestion_id or "", e.source_id), []).append(e)
    if not groups:
        step = len(evidences) / cap
        return [evidences[int(i * step)] for i in range(cap)]
    # sort keys for determinism; order groups round-robin
    ordered: list[Evidence] = []
    queues = [sorted(g, key=lambda e: (-(e.quality_score or 0), e.evidence_id))
              for _, g in sorted(groups.items())]
    while len(ordered) < cap and any(queues):
        for q in queues:
            if q and len(ordered) < cap:
                ordered.append(q.pop(0))
    return ordered


def _strip_malformed_emphasis(markdown: str) -> str:
    """PROPORZIONE: no asterischi grezzi malformati (il corsivo e' del renderer).

    Una riga di testo con un numero DISPARI di '*' non puo' contenere enfasi
    markdown valida (ogni *testo* / **testo** e' bilanciato): gli asterischi
    vengono rimossi invece di lasciare formattazione rotta. Le righe composte
    solo da asterischi/spazi (separatori tipo `***`) e l'enfasi bilanciata
    restano intatte.
    """
    fixed: list[str] = []
    for line in markdown.splitlines():
        if line.count("*") % 2 == 1 and re.search(r"[^\s*]", line):
            line = line.replace("*", "")
        fixed.append(line)
    return "\n".join(fixed)


# ---------------- allineamento citazioni per POSIZIONE (P0-2b) --------------

def _claim_parts(claim_text: str) -> tuple[str, str]:
    """(testo, punteggiatura finale) della frase del claim."""
    t = claim_text.strip()
    m = re.match(r"^(.*?)([.!?…]+)$", t, re.DOTALL)
    if m:
        return m.group(1), m.group(2)
    return t, ""


def _find_span(markdown: str, claim_text: str,
               consumed: list[tuple[int, int]]):
    """Span della frase del claim nel markdown.

    Logica tipo _claim_span del verifier, implementata localmente e consapevole
    dei marker: testa letterale + marker [n]/[a, b] (anche a META' frase,
    variante word-tolerant che non attraversa i ritorni a capo) + punteggiatura
    finale. Salta i range gia' occupati da altri claim; ritorna None se la
    frase non si ritrova.
    """
    head, punct = _claim_parts(claim_text)
    if not head.strip():
        return None
    tok = r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]"
    tail_cites = rf"(?: *{tok})*"
    tail = tail_cites + (re.escape(punct) if punct else "")
    pats = [re.compile(re.escape(head) + rf"\s*{tail}")]
    words = [re.escape(w) for w in head.split()]
    if len(words) > 1:
        gap = rf"(?:(?: +|(?:{tok}) *))+"
        pats.append(re.compile(gap.join(words) + tail))
    for pat in pats:
        for m in pat.finditer(markdown):
            if any(m.start() < e and s < m.end() for s, e in consumed):
                continue
            return m
    return None


def _rebuild_span(span_text: str, marker: str) -> str:
    """Span con un UNICO marker, a fine frase (prima della punteggiatura):
    tutti i marker [n]/[a, b] presenti nello span vengono sostituiti."""
    t = _MARKER.sub(" ", span_text)
    t = re.sub(r" {2,}", " ", t).strip()
    m = re.search(r"[.!?…]+$", t)
    if m:
        return f"{t[:m.start()].rstrip()} {marker}{m.group(0)}"
    return f"{t} {marker}".strip()


def _align_citations(markdown: str, claims: list[ReportClaim]) -> str:
    """POST-WRITE: allineamento marker [n] <-> claims per POSIZIONE.

    Il writer LLM emette marker non allineati ai citation_no del proprio JSON
    (la frase PVT porta [13] per il claim numero 3), liste [10, 20] e marker
    malformati con id interni. Prima della verifica:
    a) sanitizzazione: ogni token parentesi-quadre che NON e' una lista di
       numeri (prefissi interni ev_/clm_/src_, testo libero) e' rimosso;
    b) per ogni claim ritrovato nel markdown, il cluster di marker del suo
       span viene riscritto al citation_no del claim: il numero nel testo e'
       SEMPRE quello del claim giusto;
    c) claim senza marker nel testo: se lo span e' trovato il [citation_no]
       viene aggiunto a fine frase; se lo span NON e' trovato il claim resta
       senza marker (resta citabile in bibliografia via renumber_citations);
    d) liste fuori da ogni span: restano solo i numeri di claim esistenti,
       quelli senza claim vengono rimossi.
    """
    md = sanitize_brackets(markdown)
    valid_nos = {c.citation_no for c in claims}
    tokens = list(_MARKER.finditer(md))
    consumed: list[tuple[int, int]] = []
    edits: list[tuple[int, int, str]] = []
    for c in claims:
        m = _find_span(md, c.text, consumed)
        if m is None:
            continue  # (c) span non trovato: il claim resta senza marker
        consumed.append((m.start(), m.end()))
        edits.append((m.start(), m.end(),
                      _rebuild_span(md[m.start():m.end()],
                                    f"[{c.citation_no}]")))
    # (d) liste fuori da ogni span: numeri senza claim rimossi
    for t in tokens:
        nums = [int(x) for x in t.group(1).split(",")]
        if len(nums) < 2 or any(s <= t.start() < e for s, e in consumed):
            continue
        keep = [n for n in nums if n in valid_nos]
        if keep != nums:
            rep = "[" + ", ".join(map(str, keep)) + "]" if keep else ""
            edits.append((t.start(), t.end(), rep))
    for s, e, rep in sorted(edits, key=lambda x: x[0], reverse=True):
        md = md[:s] + rep + md[e:]
    # spazi doppi residui e spazio prima della punteggiatura
    md = re.sub(r" {2,}", " ", md)
    md = re.sub(r" ([.,;:])", r"\1", md)
    return md


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
                    *, limit_note: str = "",
                    support_stats: dict | None = None) -> DraftReport:
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
        # PROPORZIONE: frasi rese proporzionate senza asterischi grezzi malformati
        markdown = _strip_malformed_emphasis(markdown)
        # CITAZIONI (P0-2b): marker allineati ai claim per POSIZIONE prima
        # della verifica (sanitizzazione id interni, liste [a, b], marker
        # sfasati riscritti al citation_no del claim della stessa frase)
        markdown = _align_citations(markdown, claims)
        title = str(data.get("title", "")).strip() or question.strip()
        if limit_note:
            markdown += (f"\n\n> Nota: {limit_note}\n")
        if support_stats:
            # stati di verifica espliciti (dal verifier): riquadro dedicato.
            # support_stats vuoto o None -> nessuna riga aggiunta.
            markdown += (
                "\n\n> Stati delle affermazioni: "
                f"{support_stats.get('supported', 0)} supportate, "
                f"{support_stats.get('contradicted', 0)} contraddette, "
                f"{support_stats.get('insufficient', 0)} evidenze insufficienti, "
                f"{support_stats.get('mixed', 0)} contrastate\n")
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
            # PROPORZIONE: "hedged" e' facoltativo (assenza = False); tollera
            # valori non booleani senza rompere il parsing del report.
            hedged = c.get("hedged", False)
            if isinstance(hedged, str):
                hedged = hedged.strip().lower() in ("true", "1", "yes", "si", "sì")
            else:
                hedged = bool(hedged)
            claims.append(ReportClaim(text=text, citation_no=0,
                                      evidence_ids=ev_ids, hedged=hedged))
        return claims
