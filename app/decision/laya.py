"""Laya adapter: typed System-1 decisions via the `laya` Router (lazy import)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.decision.base import DecisionEngine, DecisionSpec, DecisionUnavailableError
from app.models import DecisionKind, DecisionResult

log = logging.getLogger(__name__)


def _pick(d: dict[str, Any], *names: str) -> Any:
    """First non-None value among candidate keys."""
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return None


def _confidence_of(answer: dict[str, Any]) -> float | None:
    v = _pick(answer, "answer_confidence", "confidence")
    if isinstance(v, (int, float)):
        return float(v)
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and probs:
        try:
            return float(max(float(x) for x in probs.values()))
        except (TypeError, ValueError):
            return None
    return None


class LayaDecisionEngine(DecisionEngine):
    """Decision engine backed by the `laya` package (noul/choice/score in one pass)."""

    name = "laya"

    def __init__(self, *, min_confidence: float = 0.35, model: str = "",
                 max_len: int = 0):
        self.min_confidence = min_confidence
        self.model = model
        self.max_len = max_len
        self._router: Any = None
        self._warned_no_kwargs = False

    # ---------- router lifecycle ----------
    def _get_router(self) -> Any:
        if self._router is None:
            try:
                from laya import Router  # lazy: the app works without laya installed
            except ImportError as exc:
                raise DecisionUnavailableError(
                    "laya package not installed - install with: pip install laya"
                ) from exc
            self._router = Router()
        return self._router

    # ---------- spec mapping ----------
    def _spec_to_question(self, spec: DecisionSpec) -> dict[str, Any]:
        if spec.kind == DecisionKind.BINARY:
            return {"type": "noul", "instructions": spec.question}
        if spec.kind == DecisionKind.CHOICE:
            return {"type": "choice", "instructions": spec.question,
                    "criteria": dict(spec.options or {})}
        if spec.kind == DecisionKind.SCORE:
            return {"type": "score", "instructions": spec.question,
                    "criteria": list(spec.scale or [])}
        raise DecisionUnavailableError(f"unsupported decision kind: {spec.kind}")

    def _predict(self, state: str, questions: dict[str, Any]) -> dict[str, Any]:
        """Synchronous predict; executed in a thread by decide()/decide_many()."""
        router = self._get_router()
        kwargs: dict[str, Any] = {"min_confidence": self.min_confidence}
        if self.model:
            kwargs["model"] = self.model
        if self.max_len:
            kwargs["max_len"] = self.max_len
        try:
            return router.predict(state, questions, **kwargs)
        except TypeError:
            if not self._warned_no_kwargs:
                log.warning("laya Router rejected extra kwargs; retrying with defaults")
                self._warned_no_kwargs = True
            return router.predict(state, questions)

    # ---------- answer parsing ----------
    def _result_from_answer(self, spec: DecisionSpec, answer: dict[str, Any],
                            latency_ms: float, subject_kind: str,
                            subject_id: str) -> DecisionResult:
        res = DecisionResult(
            name=spec.name, kind=spec.kind, engine=self.name,
            latency_ms=latency_ms, subject_kind=subject_kind,
            subject_id=subject_id, raw=dict(answer),
        )
        if not answer:
            res.abstained = True
            res.raw = {"error": "empty_answer"}
            return res
        if answer.get("low_confidence"):
            res.abstained = True
            return res
        if spec.kind == DecisionKind.BINARY:
            prob = _pick(answer, "noul", "probability", "prob")
            if prob is None:
                res.abstained = True
                res.raw = {"error": "missing_noul"}
                return res
            p = float(prob)
            res.value = p >= 0.5
            res.confidence = p
        elif spec.kind == DecisionKind.CHOICE:
            value = _pick(answer, "choice", "answer", "value")
            if value is None:
                res.abstained = True
                res.raw = {"error": "missing_choice"}
                return res
            res.value = str(value)
            res.confidence = _confidence_of(answer)
        else:  # SCORE
            score = _pick(answer, "score", "value", "level")
            if score is None:
                res.abstained = True
                res.raw = {"error": "missing_score"}
                return res
            res.value = int(round(float(score)))
            res.confidence = _confidence_of(answer)
        return res

    # ---------- DecisionEngine API ----------
    async def decide(self, spec: DecisionSpec, state: str, *,
                     subject_kind: str = "", subject_id: str = "") -> DecisionResult:
        t0 = time.perf_counter()
        questions = {spec.name: self._spec_to_question(spec)}
        try:
            raw = await asyncio.to_thread(self._predict, state, questions)
        except DecisionUnavailableError:
            raise
        except Exception as exc:
            raise DecisionUnavailableError(f"laya predict failed: {exc}") from exc
        latency_ms = (time.perf_counter() - t0) * 1000.0
        answers = (raw or {}).get("answers") or {}
        answer = answers.get(spec.name) or {}
        return self._result_from_answer(spec, answer, latency_ms,
                                        subject_kind, subject_id)

    async def decide_many(self, specs: list[DecisionSpec], state: str, *,
                          subject_kind: str = "", subject_id: str = "",
                          ) -> list[DecisionResult] | None:
        """Single multi-question forward pass for all specs over one state."""
        if not specs:
            return []
        t0 = time.perf_counter()
        questions = {s.name: self._spec_to_question(s) for s in specs}
        try:
            raw = await asyncio.to_thread(self._predict, state, questions)
        except DecisionUnavailableError:
            return None
        except Exception as exc:
            log.warning("laya batch predict failed: %s", exc)
            return None
        per_spec_ms = (time.perf_counter() - t0) * 1000.0 / max(1, len(specs))
        answers = (raw or {}).get("answers") or {}
        out: list[DecisionResult] = []
        for s in specs:
            answer = answers.get(s.name)
            if not answer:  # misaligned batch -> per-spec fallback upstream
                return None
            out.append(self._result_from_answer(s, answer, per_spec_ms,
                                                subject_kind, subject_id))
        return out
