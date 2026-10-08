"""Final coherence check of the report (anti-hallucination pass).

Runs at the end of the pipeline (on demand via the API): deterministic
checks (unsupported numbers, unknown URLs, uncovered factual sentences)
plus a single LLM review for unsupported/contradictory statements.
The checker NEVER raises: on any internal failure it degrades to a
permissive result and logs a warning.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.agent.verifier import _extract_json_object

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
_NUM_TOKEN = re.compile(r"\d+(?:[.,]\d+)?")
_CITE_MARK = re.compile(r"\[\d+\]")
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
# structural lines never checked: headings, blockquotes, rules, warnings
_SKIP_LINE = re.compile(
    r"^\s{0,3}#{1,6}\s|^\s{0,3}>|^\s{0,3}(-{3,}|\*{3,})|^\s{0,3}[-*]\s*⚠|⚠|\bNota\b"
    r"|non verificata|Metodo e limiti", re.IGNORECASE)
_FACTUAL = re.compile(
    r"\d|%|\bsecondo\b|\bdimostr\w+|\bstudi\b|\banalisi\b|\bdati\b", re.IGNORECASE)
_SOURCES_HEADING = re.compile(
    r"^#{1,6}\s*(fonti|sources|riferimenti|bibliografia)\b.*$", re.IGNORECASE | re.MULTILINE)
_MAX_LLM_MARKDOWN = 24000
_MAX_QUOTE_CHARS = 500


class CoherenceResult(BaseModel):
    """Outcome of the coherence check on a finished report."""
    passed: bool
    issues: list[dict] = Field(default_factory=list)  # {type, sentence, reason}
    checked_sentences: int = 0


def _strip_sources_section(markdown: str) -> str:
    """Remove the trailing bibliography section: its URLs are source links,
    not report statements, and must not trigger unknown_url."""
    m = _SOURCES_HEADING.search(markdown)
    return markdown[:m.start()] if m else markdown


def _num_tokens(text: str) -> set[str]:
    """Numeric tokens with digit-boundary guard: '20' is not found inside
    '2020' or '1.20'; decimals use the dot after comma normalization."""
    out: set[str] = set()
    for m in _NUM_TOKEN.finditer(text):
        s, e = m.span()
        before = text[s - 1] if s > 0 else " "
        after = text[e] if e < len(text) else " "
        if before.isdigit() or after.isdigit():
            continue  # slice of a longer number
        out.add(m.group(0).replace(",", "."))
    return out


def _norm(s: str) -> str:
    s = _CITE_MARK.sub(" ", s)
    s = re.sub(r"[^\w\sà-ù]", " ", s)
    return " ".join(s.lower().split())


def _sentences(markdown: str) -> list[str]:
    """Checkable sentences: structural/noise lines are skipped, bullets kept."""
    out: list[str] = []
    for line in markdown.splitlines():
        if not line.strip() or _SKIP_LINE.search(line):
            continue
        for s in re.split(r"(?<=[.!?])\s+", line):
            if s.strip():
                out.append(s.strip())
    return out


def _claim_texts(claims: list) -> list[str]:
    """Claim texts (plus corrected variants); accepts ReportClaim or dict rows."""
    out: list[str] = []
    for c in claims:
        if isinstance(c, dict):
            out.append(str(c.get("text") or ""))
            if c.get("corrected_text"):
                out.append(str(c["corrected_text"]))
        else:
            out.append(str(getattr(c, "text", "") or ""))
            ct = getattr(c, "corrected_text", None)
            if ct:
                out.append(str(ct))
    return [t for t in out if len(t.strip()) > 10]


class CoherenceChecker:
    """Deterministic + LLM coherence checks over a finished report."""

    def __init__(self, llm, prompts_dir: Path | None = None,
                 max_concurrency: int = 3):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    def _load(self) -> tuple[str, str]:
        raw = (self._prompts_dir / "coherence.txt").read_text(encoding="utf-8")
        if "---SYSTEM---" not in raw:
            raise RuntimeError("coherence.txt missing ---SYSTEM--- marker")
        system, user = raw.split("---SYSTEM---", 1)
        return system.strip(), user.strip()

    # ---------- deterministic checks ----------

    def _check_numbers(self, sentences: list[str],
                       quote_tokens: set[str]) -> list[dict]:
        issues: list[dict] = []
        for s in sentences:
            clean = _CITE_MARK.sub(" ", s)
            missing = sorted(n for n in _num_tokens(clean) if n not in quote_tokens)
            if missing:
                issues.append({
                    "type": "numero_non_supportato",
                    "sentence": s,
                    "reason": f"numeri non presenti in alcuna evidenza: {missing}",
                })
        return issues

    def _check_urls(self, sentences: list[str], quotes_blob: str) -> list[dict]:
        issues: list[dict] = []
        for s in sentences:
            for url in _URL_RE.findall(s):
                if url.rstrip(".,;:") not in quotes_blob:
                    issues.append({
                        "type": "unknown_url",
                        "sentence": s,
                        "reason": f"URL non presente nelle evidenze: {url}",
                    })
                    break
        return issues

    def _check_coverage(self, sentences: list[str],
                        claim_texts: list[str]) -> list[dict]:
        issues: list[dict] = []
        claims_norm = [_norm(t) for t in claim_texts]
        for s in sentences:
            if not _FACTUAL.search(s):
                continue
            ns = _norm(s)
            if len(ns) < 15:
                continue
            if any(ns in cn or cn in ns for cn in claims_norm if cn):
                continue
            issues.append({
                "type": "uncovered",
                "sentence": s,
                "reason": "frase fattuale non coperta da alcun claim verificato",
            })
        return issues

    # ---------- LLM check ----------

    async def _llm_issues(self, question: str, markdown: str,
                          quotes: list[str]) -> list[dict]:
        try:
            system_tpl, user_tpl = self._load()
        except Exception as exc:
            log.warning("coherence: prompt non caricabile, salto il check LLM: %s", exc)
            return []
        quotes_json = json.dumps(
            [q[:_MAX_QUOTE_CHARS] for q in quotes], ensure_ascii=False)
        user = user_tpl.format(question=question,
                               markdown=markdown[:_MAX_LLM_MARKDOWN],
                               quotes_json=quotes_json)
        for attempt in (1, 2):  # one retry on invalid output
            try:
                async with self._sem:  # bound concurrency like the other agents
                    raw = await self._llm.complete(system_tpl, user, force_json=True)
                data = _extract_json_object(raw)
                raw_issues = data.get("issues")
                if not isinstance(raw_issues, list):
                    raise ValueError("missing 'issues' list in LLM output")
                out: list[dict] = []
                for entry in raw_issues:
                    if not isinstance(entry, dict) or not str(entry.get("sentence", "")).strip():
                        continue
                    itype = str(entry.get("type", "unsupported")).strip().lower()
                    if itype not in ("unsupported", "contradiction"):
                        itype = "unsupported"
                    out.append({"type": itype,
                                "sentence": str(entry["sentence"]).strip(),
                                "reason": str(entry.get("reason", "")).strip()})
                return out
            except Exception as exc:
                log.warning("coherence: tentativo LLM %d/2 fallito: %s", attempt, exc)
                await asyncio.sleep(0.5)
        # total LLM failure: permissive result, never break the pipeline
        log.warning("coherence: check LLM non disponibile, proseguo con i soli "
                    "controlli deterministici")
        return []

    # ---------- entry point ----------

    async def check(self, question: str, markdown: str, claims: list,
                    quotes: list[str]) -> CoherenceResult:
        """Run all coherence checks; always returns a result, never raises."""
        try:
            body = _strip_sources_section(markdown or "")
            sentences = _sentences(body)
            quotes_blob = " \n".join(q or "" for q in quotes)
            quote_tokens = _num_tokens(quotes_blob)
            issues: list[dict] = []
            for check_fn, args in (
                (self._check_numbers, (sentences, quote_tokens)),
                (self._check_urls, (sentences, quotes_blob)),
                (self._check_coverage, (sentences, _claim_texts(claims))),
            ):
                try:
                    issues.extend(check_fn(*args))
                except Exception as exc:
                    log.warning("coherence: check deterministico %s fallito: %s",
                                check_fn.__name__, exc)
            issues.extend(await self._llm_issues(question or "", body,
                                                 [q for q in quotes if q]))
            return CoherenceResult(passed=not issues, issues=issues,
                                   checked_sentences=len(sentences))
        except Exception as exc:  # absolute last resort: never crash the caller
            log.exception("coherence: check fallito, risultato permissivo")
            return CoherenceResult(passed=True, issues=[],
                                   checked_sentences=0) if not markdown else \
                CoherenceResult(passed=True, issues=[{"type": "errore",
                                "sentence": "", "reason": str(exc)}],
                                checked_sentences=0)
