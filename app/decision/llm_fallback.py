"""LLM-based fallback DecisionEngine (typed JSON micro-decisions)."""
from __future__ import annotations

import json
import logging
import time

from app.decision.base import DecisionEngine, DecisionUnavailableError
from app.models import DecisionKind, DecisionResult, DecisionSpec

log = logging.getLogger(__name__)

_STATE_MAX_CHARS = 6000

_KIND_INSTRUCTIONS: dict[DecisionKind, str] = {
    DecisionKind.BINARY: (
        'Decidi se la risposta alla domanda \u00e8 VERA o FALSA rispetto allo stato. '
        'Rispondi SOLO con JSON: {"value": <true|false>, "confidence": <0..1>}'
    ),
    DecisionKind.CHOICE: (
        "Scegli ESATTAMENTE una delle opzioni elencate. "
        'Rispondi SOLO con JSON: {"value": "<una label esatta tra le opzioni>", '
        '"confidence": <0..1>}'
    ),
    DecisionKind.SCORE: (
        "Assegna un livello della scala (indice intero, 0-based, dentro i limiti). "
        'Rispondi SOLO con JSON: {"value": <int indice nella scala>, "confidence": <0..1>}'
    ),
}


class LLMDecisionEngine(DecisionEngine):
    """Runs every decision through the LLM as a strict JSON micro-prompt."""

    name = "llm"

    def __init__(self, llm):
        self._llm = llm

    def _system(self, spec: DecisionSpec) -> str:
        lines = [
            "Sei un motore di decisione tipizzato. Rispondi SOLO con un oggetto JSON "
            'con le chiavi "value" e "confidence". Nessun testo fuori dal JSON.',
            f"Tipo decisione: {spec.kind.value}",
            f"Domanda: {spec.question}",
        ]
        if spec.kind is DecisionKind.CHOICE:
            lines.append("Opzioni (etichette esatte ammesse): "
                         + json.dumps(list(spec.options or {}), ensure_ascii=False))
        elif spec.kind is DecisionKind.SCORE:
            lines.append("Scala (indici 0-based): "
                         + json.dumps(spec.scale or [], ensure_ascii=False))
        lines.append(_KIND_INSTRUCTIONS[spec.kind])
        return "\n".join(lines)

    def _user(self, spec: DecisionSpec, state: str) -> str:
        return f"STATO:\n{state[:_STATE_MAX_CHARS]}"

    def _parse(self, spec: DecisionSpec, raw: str) -> tuple[object, float | None]:
        t = raw.strip()
        if t.startswith("```"):
            t = t.strip("`")
            if t.lower().startswith("json"):
                t = t[4:]
        start = t.find("{")
        if start == -1:
            raise ValueError("no JSON object in LLM response")
        depth = 0
        in_string = False
        escaped = False
        end = -1
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
                    end = i + 1
                    break
        if end == -1:
            raise ValueError("unbalanced JSON object")
        data = json.loads(t[start:end])
        if not isinstance(data, dict):
            raise ValueError("JSON is not an object")

        confidence = data.get("confidence")
        if confidence is not None:
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
            except (TypeError, ValueError):
                confidence = None

        value = data.get("value")
        if spec.kind is DecisionKind.BINARY:
            if isinstance(value, bool):
                return value, confidence
            if isinstance(value, str):
                lv = value.strip().lower()
                if lv in ("true", "yes", "si", "s\u00ec", "1"):
                    return True, confidence
                if lv in ("false", "no", "0"):
                    return False, confidence
            raise ValueError(f"BINARY value not a bool: {value!r}")
        if spec.kind is DecisionKind.CHOICE:
            options = list(spec.options or {})
            if isinstance(value, str):
                if value in options:
                    return value, confidence
                for opt in options:
                    if opt.strip().lower() == value.strip().lower():
                        return opt, confidence
            raise ValueError(f"CHOICE value not in options: {value!r}")
        if spec.kind is DecisionKind.SCORE:
            if isinstance(value, bool):
                raise ValueError("SCORE value must be an int, got bool")
            if isinstance(value, int) and 0 <= value < len(spec.scale or []):
                return value, confidence
            raise ValueError(f"SCORE value out of range: {value!r}")
        raise ValueError(f"unsupported decision kind: {spec.kind}")

    async def decide(self, spec: DecisionSpec, state: str, *,
                     subject_kind: str = "", subject_id: str = "") -> DecisionResult:
        start = time.perf_counter()
        try:
            raw = await self._llm.complete(self._system(spec), self._user(spec, state),
                                           force_json=True)
            value, confidence = self._parse(spec, raw)
        except DecisionUnavailableError:
            raise
        except Exception as exc:
            raise DecisionUnavailableError(
                f"llm decision failed for {spec.name!r}: {exc}") from exc
        latency_ms = (time.perf_counter() - start) * 1000.0
        return DecisionResult(
            name=spec.name, kind=spec.kind, engine=self.name,
            value=value, confidence=confidence, abstained=False,
            latency_ms=latency_ms, subject_kind=subject_kind,
            subject_id=subject_id, raw={"response": raw[:2000]},
        )
