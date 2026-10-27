"""Verifier: checks every report claim against its cited evidence."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from app.models import (DraftReport, Evidence, FetchedDocument, ReportClaim,
                        Verdict, VerifiedReport, VerificationStats)

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
_VALID_VERDICTS = {"VERIFIED", "WEAKENED", "CORRECTED", "REMOVED", "FAILED"}
_VALID_SUPPORT = {"supported", "contradicted", "insufficient", "mixed"}
# Tassonomia fine delle cause (esiti insufficienti / non conclusivi): arriva
# nel verdict_reason come [cause:Y] DOPO il prefisso [support:X] — il prefisso
# resta primo e con sintassi invariata (l'orchestrator lo consuma con regex).
_VALID_CAUSES = {
    "no_relevant_evidence",   # nessuna evidenza pertinente recuperata
    "evidence_insufficient",  # pertinente ma non sufficiente a decidere
    "doc_missing",            # documento non acquisito o illeggibile
    "truncated",              # testo troncato o estrazione incompleta
    "overstated",             # affermazione piu' forte della fonte
    "contradiction",          # contraddizione tra evidenze
    "technical_error",        # timeout, rate limit, errore API
    "invalid_output",         # output del verificatore non valido
}
_SUPPORT_REASON = "evidenza contraddice"
# Errore tecnico / output non valido: il claim NON e' "falso", e' NON
# verificato. Il motivo dice sempre "verifica non conclusa" per non far
# passare un problema di pipeline per un esito di merito.
_NON_CONCLUSIVE = "verifica non conclusa"
# Istruzione di seconda verifica quando il template dedicato non esiste nel
# verifier.txt in uso (prompts minimali dei test): la formulazione corretta
# viene riverificata da sola contro le stesse evidenze.
_RECHECK_INSTR = (
    "\n\nSECOND VERIFICATION - CORRECTED FORMULATION ONLY.\n"
    "The original claim was already rewritten as:\n"
    "\"{corrected_text}\"\n"
    "Verify ONLY this corrected formulation against the SAME evidence.\n"
    "- verdict VERIFIED only if the corrected formulation is directly and "
    "fully supported by the evidence;\n"
    "- otherwise WEAKENED (or FAILED if verification cannot be performed): "
    "a vaguer sentence is not automatically supported.\n"
    "Respond with the same JSON shape (corrected_text: null)."
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_CITE_TOKEN = re.compile(r"\[\d+\]")
_TRAILING_PUNCT = re.compile(r"[.!?…]+")

# ---- guardie deterministiche a valle (P0-4) --------------------------------
# Il verifier LLM controlla numeri e contraddizioni, NON lo scope: senza queste
# regole in codice un claim che allarga la popolazione ("benefici
# sull'attenzione" da una quote su "bambini con ADHD") passava VERIFIED, e un
# fatto forte citato da una fonte divulgativa passava senza attribuzione.
# Tipi di fonte PRIMARI (allineati a gap_checker.PRIMARY_SOURCE_TYPES e alle
# opzioni della decisione source_type dell'orchestrator): tutto il resto
# (news/forum/other/...) e' SECONDARIO. source_type ignoto -> non blocca.
_PRIMARY_SOURCE_TYPES = {"official", "paper", "primaria", "primary",
                         "studio", "ufficiale"}
# fatto scientifico FORTE: stem it/en; una fonte secondaria da sola non puo'
# sostenere questi verbi (serve attribuzione "secondo la fonte")
_STRONG_FACT_RE = re.compile(
    r"dimostr|prova che|provi che|scientificament|scientifically|demonstrat|"
    r"proves that|proven that|conclusivament", re.IGNORECASE)
# meta-affermazioni sullo STATO delle evidenze: ammesse solo se il reason
# cita evidenze concrete in conflitto/concordi (altrimenti e' il bug
# "dichiarare evidenze contrastanti senza base")
_META_CLAIM_RE = re.compile(
    r"contrastant|contraddittor|contraddic|discord|concord|converg|coerent|"
    r"diverg", re.IGNORECASE)
_META_STOPWORDS = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "del",
    "della", "dello", "dei", "delle", "degli", "e", "ed", "o", "in", "su",
    "per", "con", "tra", "fra", "che", "si", "non", "al", "allo", "alla",
    "ai", "agli", "alle", "da", "dal", "the", "of", "a", "an", "and", "on",
    "to", "is", "are", "that", "this", "it", "as", "be", "by", "with",
    "for", "at", "was", "were",
}


def _has_primary_source(evidence_ids: list[str],
                        ev_map: dict[str, Evidence]) -> bool:
    for eid in evidence_ids:
        st = (getattr(ev_map.get(eid), "source_type", None) or "").strip().lower()
        if st in _PRIMARY_SOURCE_TYPES:
            return True
    return False


def _has_secondary_source(evidence_ids: list[str],
                          ev_map: dict[str, Evidence]) -> bool:
    for eid in evidence_ids:
        ev = ev_map.get(eid)
        st = (getattr(ev, "source_type", None) or "").strip().lower()
        if ev is not None and st and st not in _PRIMARY_SOURCE_TYPES:
            return True
    return False


def _population_scope_gap(claim_text: str, evidence_ids: list[str],
                          ev_map: dict[str, Evidence]) -> set[str]:
    """Classi di popolazione dichiarate dalle evidenze e del tutto assenti
    dal claim (euristica deterministica, lista EDITABILE in
    reader.POPULATION_CLASSES). Restringere la popolazione e' lecito: il gap
    esiste solo quando il claim non mantiene NESSUNA classe citata."""
    from app.agent.reader import population_groups  # euristica condivisa
    required: set[str] = set()
    for eid in evidence_ids:
        ev = ev_map.get(eid)
        if ev is not None:
            required |= population_groups(f"{ev.quote} {ev.claim}")
    if not required:
        return set()
    present = population_groups(claim_text)
    return required if not (present & required) else set()


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower(), re.UNICODE)


def _reason_cites_evidence(reason: str, evidence_ids: list[str],
                           ev_map: dict[str, Evidence]) -> bool:
    """Il reason riferisce evidenze CONCRETE (id citato, oppure n-gramma di
    contenuto ripreso dal claim/quote di un'evidenza citata)? Serve a
    bloccare le meta-affermazioni vuote ("le evidenze sono contrastanti")
    senza base: l'id delle evidenze e' nel prompt, il reason deve usarlo o
    riportarne il contenuto."""
    rl = reason.lower()
    for eid in evidence_ids:
        if eid.lower() in rl:
            return True
    r_words = _words(reason)
    r_grams = {" ".join(r_words[i:i + 3]) for i in range(len(r_words) - 2)}
    if not r_grams:
        return False
    for eid in evidence_ids:
        ev = ev_map.get(eid)
        if ev is None:
            continue
        for src in (ev.claim, ev.quote):
            w = _words(src)
            for i in range(len(w) - 2):
                gram = " ".join(w[i:i + 3])
                if (gram in r_grams
                        and not all(t in _META_STOPWORDS for t in gram.split())):
                    return True
    return False


class _InvalidOutputError(ValueError):
    """Output del verifier LLM non utilizzabile (JSON o verdict invalido)."""


def _claim_parts(claim_text: str) -> tuple[str, str]:
    t = claim_text.strip()
    m = re.match(r"^(.*?)([.!?…]+)$", t, re.DOTALL)
    if m:
        return m.group(1), m.group(2)
    return t, ""


def _claim_span(markdown: str, claim_text: str):
    """Locate a claim tolerating displaced punctuation and [n] citations.

    Writer output varies: 'frase.' / 'frase [1][2].' / 'frase. [1][2]' —
    match head + optional citations + optional trailing punctuation.
    """
    head, punct = _claim_parts(claim_text)
    tail_pat = r"(?:\s*\[\d+\])*" + (re.escape(punct) if punct else "")
    return re.search(re.escape(head) + r"\s*" + tail_pat, markdown)


def _replace_claim(markdown: str, claim_text: str, new_text: str) -> str | None:
    """Replace the claim, re-attaching its citations and sentence punctuation."""
    m = _claim_span(markdown, claim_text)
    if m is None:
        return None
    group = m.group(0)
    cites = "".join(_CITE_TOKEN.findall(group))
    punct = _TRAILING_PUNCT.search(_claim_parts(claim_text)[1])
    rebuilt = new_text.strip()
    if punct:
        rebuilt = rebuilt.rstrip(_TRAILING_PUNCT.pattern.strip("[]+")) + punct.group(0)
    if cites:
        rebuilt += " " + cites
    return markdown[:m.start()] + rebuilt + markdown[m.end():]


def _remove_claim(markdown: str, claim_text: str) -> str | None:
    m = _claim_span(markdown, claim_text)
    if m is None:
        return None
    return markdown[:m.start()] + markdown[m.end():]


def _numbers(text: str) -> set[str]:
    """Normalized numeric TOKENS (int/decimal, comma or dot) — token-boundary
    aware: '20' in 'nel 2020' is 2020's token, not 20 (P0-3 fix)."""
    return {m.group(0).replace(",", ".") for m in _NUMBER_RE.finditer(text)}


def _num_tokens(text: str) -> set[str]:
    """Number tokens with word boundaries: '20' does NOT match inside '2020'
    or '1.20' (percentages like 3,5 are one token)."""
    out: set[str] = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)?", text):
        s, e = m.span()
        before = text[s - 1] if s > 0 else " "
        after = text[e] if e < len(text) else " "
        if before.isdigit() or after.isdigit():
            continue  # this match is a slice of a longer number
        out.add(m.group(0).replace(",", "."))
    return out


def _norm_md(s: str) -> str:
    """Normalization for the fallback match: no [n] markers, collapsed
    whitespace, lowercase, punctuation stripped."""
    s = re.sub(r"\[\d+\]", " ", s)
    s = re.sub(r"[^\w\sà-ù]", " ", s)
    return " ".join(s.lower().split())


def _fuzzy_find(markdown: str, claim_text: str) -> str | None:
    """Fallback B2: normalized substring; then segment-similarity for merged
    sentences (claim = fusion of two markdown sentences). Returns the matched
    markdown fragment or None."""
    n_md = _norm_md(markdown)
    n_claim = _norm_md(claim_text)
    if n_claim and n_claim in n_md:
        return claim_text
    words = n_claim.split()
    if len(words) < 8:
        return None
    from difflib import SequenceMatcher
    segments = [s for s in re.split(r"(?<=[.!?])\s+|\n", markdown) if s.strip()]
    n_segments = [_norm_md(s) for s in segments]
    for frac in (1.0, 0.5):
        w = max(4, int(len(words) * frac))
        for i in range(0, max(1, len(words) - w + 1)):
            piece = " ".join(words[i:i + w])
            for seg, ns in zip(segments, n_segments):
                if len(ns) < 10:
                    continue
                if SequenceMatcher(None, piece, ns).ratio() >= 0.85:
                    return seg.strip()
    return None


def _extract_json_object(text: str) -> dict:
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
                data = json.loads(t[start:i + 1])
                if not isinstance(data, dict):
                    raise ValueError("JSON is not an object")
                return data
    raise ValueError("unbalanced JSON object")


def _default_cause(support: str, verdict: str,
                   ctx: str | None) -> str | None:
    """Causa deterministica quando l'LLM non ne fornisce una valida.

    Un esito insufficiente con contesto rotto (documento mancante o quote non
    nel sorgente) e' prima di tutto un problema di contesto; per gli esiti di
    merito (contraddetto/misto) la causa e' il merito, il contesto dopo.
    """
    if support == "insufficient":
        if ctx in ("doc_missing", "truncated"):
            return ctx
        return "evidence_insufficient"
    if verdict == "REMOVED":
        return "no_relevant_evidence"
    if support in ("mixed", "contradicted"):
        return "contradiction"
    if ctx in ("doc_missing", "truncated"):
        return ctx
    if verdict in ("WEAKENED", "CORRECTED"):
        return "overstated"
    return None  # esito pulito (VERIFIED+supported): nessuna causa da aggiungere


class Verifier:
    """Checks every claim against its cited evidence.

    Support-state convention: the LLM also returns an explicit support state
    (supported | contradicted | insufficient | mixed). It is NOT stored on
    ReportClaim (models.py is frozen): it is recorded as a structured prefix
    in `claim.verdict_reason` -> "[support:contradicted] motivo ...".
    Consumers must parse the prefix, never free text. Default when the LLM
    omits it: "insufficient". support=contradicted on a VERIFIED claim
    degrades the verdict to WEAKENED (evidence outweighs the model's ok).

    Cause taxonomy: quando l'esito non e' pulito, il verdict_reason porta
    anche "[cause:Y]" SUBITO DOPO il prefisso [support:X], con Y nella
    tassonomia _VALID_CAUSES (dal campo "cause" della risposta LLM, altrimenti
    determinato da _default_cause). Gli errori tecnici (timeout, rate limit,
    errori API, output non valido) producono FAILED con causa technical_error
    o invalid_output e motivo "verifica non conclusa": MAI un esito di merito.

    Second verification: un CORRECTED con corrected_text non e' accettato
    cosi' com'e' — la SOLA formulazione corretta viene riverificata una volta
    contro le stesse evidenze; se non e' supportata anche lei, il claim
    degrada a WEAKENED (rendere piu' vaga una frase non la rende supportata).
    Se la seconda verifica non e' eseguibile (errore tecnico) resta valido il
    CORRECTED della prima verifica.

    Guardie deterministiche a valle (P0-4, dopo l'esito LLM; il prefisso
    [support:X] resta INVARIATO, la causa va in tassonomia):
    1. POPOLAZIONE: evidenze con popolazione esplicita (reader.POPULATION_
       CLASSES: adhd, bambini, pazienti, ...) e claim che non ne mantiene
       NESSUNA -> WEAKENED [cause:overstated] (restringere e' lecito,
       genericizzare no).
    2. ATTRIBUZIONE: fatto scientifico forte ("dimostra", "prova che", ...)
       sostenuto SOLO da fonti non primarie (source_type news/forum/other;
       primarie: official/paper) -> verdict massimo WEAKENED, reason
       "fonte secondaria: richiede attribuzione".
    3. META-CLAIMS: claim sullo STATO delle evidenze ("contrastanti",
       "concordano") il cui reason non cita evidenze concrete ->
       WEAKENED "meta-affermazione non supportata".
    """
    def __init__(self, llm, prompts_dir: Path | None = None,
                 max_concurrency: int = 3):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    def _load(self) -> tuple[str, str, str | None]:
        raw = (self._prompts_dir / "verifier.txt").read_text(encoding="utf-8")
        if "---SYSTEM---" not in raw:
            raise RuntimeError("verifier.txt missing ---SYSTEM--- marker")
        system, rest = raw.split("---SYSTEM---", 1)
        recheck: str | None = None
        if "---RECHECK---" in rest:
            rest, recheck = rest.split("---RECHECK---", 1)
            recheck = recheck.strip() or None
        return system.strip(), rest.strip(), recheck

    @staticmethod
    def _evidence_json(claim: ReportClaim,
                       ev_map: dict[str, Evidence]) -> str:
        return json.dumps(
            [{"evidence_id": eid, "claim": ev_map[eid].claim,
              "quote": ev_map[eid].quote[:300]}
             for eid in claim.evidence_ids if eid in ev_map],
            ensure_ascii=False)

    def _unsupported_numbers(self, text: str, evidence_ids: list[str],
                             ev_map: dict[str, Evidence]) -> set[str]:
        """Numbers in `text` not present in the cited evidence texts.
        Token-boundary aware; evidence text = quote + generated summary
        (both: the summary can paraphrase numbers absent from the quote)."""
        quoted = " \n".join(
            (ev_map[eid].quote + " \n" + ev_map[eid].claim)
            for eid in evidence_ids if eid in ev_map
        ).lower()
        quoted_tokens = _num_tokens(quoted)
        return {n for n in _num_tokens(text) if n not in quoted_tokens}

    async def _llm_verdict(self, system_tpl: str, user_tpl: str,
                           question: str, claim: ReportClaim,
                           ev_map: dict[str, Evidence]
                           ) -> tuple[Verdict, str, str | None, str, str | None]:
        ev_json = self._evidence_json(claim, ev_map)
        user = user_tpl.format(question=question, claim=claim.text,
                               evidence_json=ev_json)
        raw = await self._llm.complete(system_tpl, user, force_json=True)
        try:
            data = _extract_json_object(raw)
            verdict = str(data.get("verdict", "")).upper()
            if verdict not in _VALID_VERDICTS:
                raise ValueError(f"invalid verdict: {verdict!r}")
        except ValueError as exc:
            raise _InvalidOutputError(str(exc)) from exc
        # support state (explicit verification states): default "insufficient"
        # when absent or invalid — never fail a whole verification for it.
        support = str(data.get("support", "insufficient")).strip().lower()
        if support not in _VALID_SUPPORT:
            log.debug("verifier: invalid support %r -> insufficient", support)
            support = "insufficient"
        # cause dalla tassonomia: assente/invalida -> None (la determina poi
        # _default_cause); mai fallire la verifica per un campo diagnostico.
        cause = str(data.get("cause") or "").strip().lower()
        if cause not in _VALID_CAUSES:
            cause = None
        reason = str(data.get("reason", ""))
        corrected = data.get("corrected_text")
        corrected = str(corrected).strip() if corrected else None
        return verdict, reason, corrected, support, cause  # type: ignore[return-value]

    async def _recheck_corrected(
            self, system_tpl: str, user_tpl: str, recheck_tpl: str | None,
            question: str, claim: ReportClaim, corrected: str,
            ev_map: dict[str, Evidence]
            ) -> tuple[str, str, str, str | None] | None:
        """Seconda verifica della SOLA formulazione corretta contro le stesse
        evidenze. Ritorna (verdict, support, reason, cause), oppure None se la
        seconda verifica non e' eseguibile (errore tecnico): in quel caso
        resta valido il verdict CORRECTED della prima verifica — non si
        degrada senza un esito."""
        ev_json = self._evidence_json(claim, ev_map)
        if recheck_tpl:
            try:
                user = recheck_tpl.format(
                    question=question, claim=claim.text,
                    corrected_text=corrected, evidence_json=ev_json)
            except (KeyError, IndexError, ValueError):
                user = (f"{claim.text}\n\n"
                        + _RECHECK_INSTR.format(corrected_text=corrected)
                        + f"\n\nEvidence (JSON):\n{ev_json}")
        else:
            user = (user_tpl.format(question=question, claim=claim.text,
                                    evidence_json=ev_json)
                    + "\n\n" + _RECHECK_INSTR.format(corrected_text=corrected))
        try:
            raw = await self._llm.complete(system_tpl, user, force_json=True)
            data = _extract_json_object(raw)
        except Exception as exc:
            log.warning("verifier: seconda verifica della formulazione "
                        "corretta non riuscita su %s: %s (mantengo il "
                        "verdict CORRECTED della prima verifica)",
                        claim.claim_id, exc)
            return None
        r_verdict = str(data.get("verdict", "")).upper()
        r_support = str(data.get("support", "insufficient")).strip().lower()
        if r_support not in _VALID_SUPPORT:
            r_support = "insufficient"
        r_cause = str(data.get("cause") or "").strip().lower()
        if r_cause not in _VALID_CAUSES:
            r_cause = None
        r_reason = str(data.get("reason", ""))
        return r_verdict, r_support, r_reason, r_cause

    async def verify(self, question: str, draft: DraftReport,
                     ev_map: dict[str, Evidence],
                     sources: dict[str, FetchedDocument]) -> VerifiedReport:
        system_tpl, user_tpl, recheck_tpl = self._load()
        markdown = draft.markdown
        support_counts = {s: 0 for s in sorted(_VALID_SUPPORT)}
        ctx_cache: dict[str, str] = {}   # evidence_id -> stato contesto
        norm_docs: dict[str, str] = {}   # source_id -> testo normalizzato

        def context_status(eid: str, claim_id: str) -> str:
            """Diagnosi del contesto passato al prompt: la quote dell'evidenza
            DEVE trovarsi nel testo del documento sorgente. Se non c'e', il
            contesto e' sbagliato o l'estrazione incompleta -> warning con
            evidence_id, diagnosticabile dopo (falsi negativi nascosti)."""
            st = ctx_cache.get(eid)
            if st is not None:
                return st
            ev = ev_map[eid]
            doc = sources.get(ev.source_id)
            if doc is None or not (doc.text or "").strip():
                st = "doc_missing"
            else:
                nd = norm_docs.get(ev.source_id)
                if nd is None:
                    nd = norm_docs[ev.source_id] = _norm_md(doc.text)
                st = "ok" if _norm_md(ev.quote) in nd else "truncated"
            ctx_cache[eid] = st
            if st != "ok":
                log.warning(
                    "verifier: quote dell'evidenza %s non presente nel "
                    "documento sorgente %s (%s): contesto sbagliato o "
                    "estrazione incompleta (claim %s)",
                    eid, ev.source_id, st, claim_id)
            return st

        async def one(claim: ReportClaim) -> ReportClaim:
            nonlocal markdown
            if not claim.evidence_ids or any(e not in ev_map for e in claim.evidence_ids):
                claim.verdict, claim.verdict_reason = (
                    "REMOVED",
                    "[support:insufficient] [cause:no_relevant_evidence] "
                    "nessuna evidenza valida recuperata per il claim")
                new_md = _remove_claim(markdown, claim.text)
                if new_md is not None:
                    markdown = new_md
                return claim
            bad_numbers = self._unsupported_numbers(
                claim.text, claim.evidence_ids, ev_map)
            async with self._sem:  # bounded concurrency: providers rate-limit bursts
                try:
                    verdict, reason, corrected, support, cause = await self._llm_verdict(
                        system_tpl, user_tpl, question, claim, ev_map)
                except _InvalidOutputError as exc:
                    # output non valido: NON e' un esito di merito sul claim
                    log.warning("verifier: output non valido su %s: %s",
                                claim.claim_id, exc)
                    claim.verdict, claim.verdict_reason = (
                        "FAILED",
                        f"[support:insufficient] [cause:invalid_output] "
                        f"{_NON_CONCLUSIVE}: output del verifier non "
                        f"valido ({exc})")
                    return claim
                except Exception as exc:
                    # timeout / rate limit / errore API: il claim NON e' falso
                    log.warning("verifier: LLM check failed on %s: %s",
                                claim.claim_id, exc)
                    claim.verdict, claim.verdict_reason = (
                        "FAILED",
                        f"[support:insufficient] [cause:technical_error] "
                        f"{_NON_CONCLUSIVE}: errore tecnico ({exc})")
                    return claim
                # corrected_text vuoto con CORRECTED -> retry esplicito (B2)
                if verdict == "CORRECTED" and not (corrected or "").strip():
                    for _retry in range(2):
                        try:
                            (verdict, reason, corrected, support,
                             cause) = await self._llm_verdict(
                                system_tpl, user_tpl, question, claim, ev_map)
                            if (corrected or "").strip():
                                break
                        except Exception as exc:
                            log.warning("verifier: retry %s su %s: %s",
                                        _retry + 1, claim.claim_id, exc)
                            await asyncio.sleep(1)
                if verdict == "CORRECTED" and not (corrected or "").strip():
                    # il modello non ha prodotto il testo corretto nemmeno dopo
                    # i retry: NON e' una verifica riuscita -> non conclusiva
                    verdict, cause = "FAILED", "invalid_output"
                    reason = (
                        f"{_NON_CONCLUSIVE}: il verifier ha richiesto una "
                        "correzione ma il testo corretto non e' stato "
                        "prodotto (corrected_text vuoto)")
                # -- seconda verifica della formulazione corretta: CORRECTED
                # rende la frase piu' cauta, non piu' vera. La SOLA
                # formulazione corretta viene riverificata UNA volta sulle
                # stesse evidenze; se non e' supportata anche lei -> WEAKENED.
                if verdict == "CORRECTED" and (corrected or "").strip():
                    rechecked = await self._recheck_corrected(
                        system_tpl, user_tpl, recheck_tpl, question, claim,
                        corrected, ev_map)
                    if rechecked is not None:
                        r_verdict, r_support, r_reason, r_cause = rechecked
                        bad_corr = self._unsupported_numbers(
                            corrected, claim.evidence_ids, ev_map)
                        if (r_verdict == "VERIFIED" and r_support == "supported"
                                and not bad_corr):
                            cause = cause or _default_cause(support, verdict, None)
                            reason += (" (formulazione corretta confermata "
                                       "dalla seconda verifica)")
                        else:
                            verdict, support = "WEAKENED", r_support
                            corrected = None  # nessuna sostituzione nel markdown
                            detail = "; ".join(x for x in (
                                f"seconda verifica: {r_reason}" if r_reason else "",
                                (f"valori {sorted(bad_corr)} non presenti "
                                 "nelle evidenze") if bad_corr else "",
                            ) if x)
                            reason = ("anche la formulazione corretta non e' "
                                      "supportata dalle evidenze (seconda "
                                      "verifica)"
                                      + (f": {detail}" if detail else ""))
                            cause = r_cause or _default_cause(
                                support, "WEAKENED", None)
                if bad_numbers and verdict == "VERIFIED":
                    verdict = "WEAKENED"
                    reason = (f"numeric values {sorted(bad_numbers)} not present in "
                              f"quoted evidence; " + reason)
                    cause = cause or _default_cause(support, verdict, None)
                if support == "contradicted" and verdict == "VERIFIED":
                    # lo stato di verifica esplicito prevale: l'evidenza nega la
                    # affermazione, un VERIFIED non e' credibile -> WEAKENED
                    verdict, reason = "WEAKENED", _SUPPORT_REASON
                    cause = cause or _default_cause(support, verdict, None)
                # -- guardie deterministiche a valle (P0-4): l'LLM controlla
                # numeri e contraddizioni, NON lo scope. Il prefisso [support:X]
                # resta INVARIATO (descrive le evidenze, non il verdetto); la
                # causa e' in tassonomia ([cause:overstated]).
                if verdict == "VERIFIED":
                    gap = _population_scope_gap(claim.text, claim.evidence_ids,
                                                ev_map)
                    if gap:
                        # (a) POPOLAZIONE: evidenze con popolazione esplicita
                        # (es. "bambini/adolescenti ADHD"), claim generico che
                        # la omette del tutto -> scope allargato -> WEAKENED
                        log.info("verifier: guardia popolazione su %s: "
                                 "popolazioni %s presenti nelle evidenze e "
                                 "omesse dal claim", claim.claim_id,
                                 ", ".join(sorted(gap)))
                        verdict = "WEAKENED"
                        reason = ("claim generico: le evidenze dichiarano la "
                                  "popolazione (" + ", ".join(sorted(gap))
                                  + ") che il claim omette del tutto: scope "
                                  "allargato" + (f"; {reason}" if reason else ""))
                        cause = "overstated"
                if verdict in ("VERIFIED", "CORRECTED"):
                    published = (corrected if verdict == "CORRECTED"
                                 and (corrected or "").strip() else claim.text)
                    if (_STRONG_FACT_RE.search(published)
                            and _has_secondary_source(
                                claim.evidence_ids, ev_map)
                            and not _has_primary_source(
                                claim.evidence_ids, ev_map)):
                        # (b) ATTRIBUZIONE: fatto scientifico forte ("dimostra",
                        # "prova che", ...) sostenuto SOLO da fonti secondarie
                        # (news/forum/other) -> verdict massimo WEAKENED, il
                        # writer formulera' con l'attribuzione alla fonte
                        log.info("verifier: guardia attribuzione su %s: "
                                 "fatto forte su fonte non primaria",
                                 claim.claim_id)
                        verdict = "WEAKENED"
                        corrected = None
                        reason = ("fonte secondaria: richiede attribuzione — "
                                  "fatto scientifico forte non sostenibile da "
                                  "sole fonti non primarie"
                                  + (f"; {reason}" if reason else ""))
                        cause = cause or "overstated"
                    elif (_META_CLAIM_RE.search(published)
                          and not _reason_cites_evidence(
                              reason, claim.evidence_ids, ev_map)):
                        # (c) META-CLAIMS: il claim descrive lo stato delle
                        # evidenze ("contrastanti", "concordano") ma il reason
                        # NON cita nessuna evidenza concreta -> WEAKENED
                        log.info("verifier: guardia meta-claim su %s: "
                                 "stato delle evidenze affermato senza "
                                 "riferimenti concreti", claim.claim_id)
                        verdict = "WEAKENED"
                        corrected = None
                        reason = ("meta-affermazione non supportata: il claim "
                                  "descrive lo stato delle evidenze senza "
                                  "citare evidenze concrete in conflitto/"
                                  "concordi" + (f"; {reason}" if reason else ""))
                        cause = cause or "overstated"
                # diagnosi contesto (warning su quote non nel sorgente) + causa
                # per gli esiti non puliti
                ctx = "ok"
                for eid in claim.evidence_ids:
                    st = context_status(eid, claim.claim_id)
                    if st != "ok" and ctx == "ok":
                        ctx = st
                cause = cause or _default_cause(support, verdict, ctx)
                # convenzione [support:X] + tassonomia [cause:Y] DOPO il
                # prefisso (vedi docstring di Verifier; models.py e' congelato)
                cause_seg = f"[cause:{cause}] " if cause else ""
                claim.verdict, claim.verdict_reason = (
                    verdict, f"[support:{support}] {cause_seg}{reason}".strip())
                support_counts[support] += 1
                if verdict == "CORRECTED" and corrected:
                    new_md = _replace_claim(markdown, claim.text, corrected)
                    if new_md is None and _fuzzy_find(markdown, claim.text):
                        # fallback normalizzato: sostituisco il frammento trovato
                        frag = _fuzzy_find(markdown, claim.text)
                        new_md = _replace_claim(markdown, frag, corrected)
                    if new_md is not None:
                        markdown = new_md
                        claim.corrected_text = corrected
                    else:
                        # B2: MAI silenzio -> frase marcata NON VERIFICATA nel
                        # report; il claim resta dichiarato non conclusivo
                        claim.verdict = "FAILED"
                        claim.verdict_reason = (
                            f"[support:{support}] [cause:technical_error] "
                            f"{_NON_CONCLUSIVE}: frase del claim non "
                            "ritrovata nel markdown (nemmeno con matching "
                            "normalizzato): citazione segnalata come non "
                            "verificata")
                        markdown = markdown.replace(claim.text,
                            claim.text + " ⚠ *non verificata*", 1) \
                            if claim.text in markdown else markdown
                elif verdict == "REMOVED":
                    new_md = _remove_claim(markdown, claim.text)
                    if new_md is not None:
                        markdown = new_md
            return claim

        claims = list(await asyncio.gather(*(one(c) for c in draft.claims)))

        # aggregazione stati di verifica espliciti (log + base per support_stats)
        total_support = sum(support_counts.values())
        if total_support:
            summary = ", ".join(f"{k}={v}" for k, v in support_counts.items())
            log.info("verifier: support stats (%d claims): %s",
                     total_support, summary)

        stats = VerificationStats(number_of_claims=len(claims))
        for c in claims:
            if c.verdict == "VERIFIED":
                stats.number_verified += 1
            elif c.verdict == "CORRECTED":
                stats.number_corrected += 1
            elif c.verdict == "REMOVED":
                stats.number_removed += 1
            else:
                stats.number_failed += 1  # WEAKENED counted as failed-check too

        # ---- P0-3: text -> claim coverage (every factual sentence must be a
        # claim). Sentences in the final markdown not matched by any claim are
        # flagged inline and counted; no silent unverified prose survives.
        from app.agent.coverage import uncovered_sentences, mark_uncovered
        covered_texts = {c.text for c in claims} | {
            c.corrected_text for c in claims if c.corrected_text}
        uncovered = uncovered_sentences(markdown, covered_texts)
        stats.number_uncovered = len(uncovered)
        markdown = mark_uncovered(markdown, uncovered)

        # ---- P0-2: unified citation numbering on the VERIFIED text ----
        from app.agent.citations import renumber_citations
        markdown, claims = renumber_citations(markdown, claims)
        return VerifiedReport(markdown=markdown, claims=claims, stats=stats)
