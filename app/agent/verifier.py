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
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    """Normalized numeric tokens (int/decimal, comma or dot)."""
    return {m.group(0).replace(",", ".") for m in _NUMBER_RE.finditer(text)}


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


class Verifier:
    def __init__(self, llm, prompts_dir: Path | None = None,
                 max_concurrency: int = 3):
        self._llm = llm
        self._prompts_dir = prompts_dir if prompts_dir is not None else _PROMPTS_DIR
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    def _load(self) -> tuple[str, str]:
        raw = (self._prompts_dir / "verifier.txt").read_text(encoding="utf-8")
        if "---SYSTEM---" not in raw:
            raise RuntimeError("verifier.txt missing ---SYSTEM--- marker")
        system, user = raw.split("---SYSTEM---", 1)
        return system.strip(), user.strip()

    def _unsupported_numbers(self, claim: ReportClaim,
                             ev_map: dict[str, Evidence]) -> set[str]:
        quoted = " ".join(
            (ev_map[eid].quote + " " + ev_map[eid].claim)
            for eid in claim.evidence_ids if eid in ev_map
        ).lower()
        return {n for n in _numbers(claim.text) if n not in quoted}

    async def _llm_verdict(self, system_tpl: str, user_tpl: str,
                           question: str, claim: ReportClaim,
                           ev_map: dict[str, Evidence]) -> tuple[Verdict, str, str | None]:
        ev_json = json.dumps(
            [{"evidence_id": eid, "claim": ev_map[eid].claim,
              "quote": ev_map[eid].quote[:300]}
             for eid in claim.evidence_ids if eid in ev_map],
            ensure_ascii=False)
        user = user_tpl.format(question=question, claim=claim.text,
                               evidence_json=ev_json)
        raw = await self._llm.complete(system_tpl, user, force_json=True)
        data = _extract_json_object(raw)
        verdict = str(data.get("verdict", "")).upper()
        if verdict not in _VALID_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict!r}")
        reason = str(data.get("reason", ""))
        corrected = data.get("corrected_text")
        corrected = str(corrected).strip() if corrected else None
        return verdict, reason, corrected  # type: ignore[return-value]

    async def verify(self, question: str, draft: DraftReport,
                     ev_map: dict[str, Evidence],
                     sources: dict[str, FetchedDocument]) -> VerifiedReport:
        system_tpl, user_tpl = self._load()
        markdown = draft.markdown

        async def one(claim: ReportClaim) -> ReportClaim:
            nonlocal markdown
            if not claim.evidence_ids or any(e not in ev_map for e in claim.evidence_ids):
                claim.verdict, claim.verdict_reason = "REMOVED", "no valid evidence"
                if claim.text in markdown:
                    markdown = markdown.replace(claim.text, "", 1)
                return claim
            bad_numbers = self._unsupported_numbers(claim, ev_map)
            async with self._sem:  # bounded concurrency: providers rate-limit bursts
                try:
                    verdict, reason, corrected = await self._llm_verdict(
                        system_tpl, user_tpl, question, claim, ev_map)
                except Exception as exc:
                    log.warning("verifier: LLM check failed on %s: %s",
                                claim.claim_id, exc)
                    claim.verdict, claim.verdict_reason = (
                        "FAILED", f"verification error: {exc}")
                    return claim
            if bad_numbers and verdict == "VERIFIED":
                verdict = "WEAKENED"
                reason = (f"numeric values {sorted(bad_numbers)} not present in "
                          f"quoted evidence; " + reason)
            claim.verdict, claim.verdict_reason = verdict, reason
            if verdict == "CORRECTED" and corrected:
                if claim.text in markdown:
                    markdown = markdown.replace(claim.text, corrected, 1)
                    claim.corrected_text = corrected
                else:
                    claim.verdict, claim.verdict_reason = "FAILED", "corrected sentence not found in markdown"
            elif verdict == "REMOVED" and claim.text in markdown:
                markdown = markdown.replace(claim.text, "", 1)
            return claim

        claims = list(await asyncio.gather(*(one(c) for c in draft.claims)))

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
        return VerifiedReport(markdown=markdown, claims=claims, stats=stats)
