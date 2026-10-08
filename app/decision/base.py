"""DecisionEngine contract and routing facade with LLM fallback."""
from __future__ import annotations

import abc
import json
import logging
import threading
import time

from app.models import DecisionResult, DecisionSpec

log = logging.getLogger(__name__)


class DecisionUnavailableError(RuntimeError):
    """A decision engine cannot run (missing lib, load failure, inference crash)."""


class DecisionEngine(abc.ABC):
    """Contract for typed System-1 decisions over a text state."""

    name: str = "base"

    @abc.abstractmethod
    async def decide(self, spec: DecisionSpec, state: str, *,
                     subject_kind: str = "", subject_id: str = "") -> DecisionResult:
        """Decide a single typed question over `state`."""

    async def decide_many(self, specs: list[DecisionSpec], state: str, *,
                          subject_kind: str = "", subject_id: str = "",
                          ) -> list[DecisionResult] | None:
        """Batch hook: all specs over the same state. None = cannot batch."""
        return None


class DecisionRouter:
    """Routes decisions to the primary engine with guaranteed fallback; records all.

    Concurrency (P0-4): engines are shared (stateless/thread-safe at the call
    level); run_id and counts are PER-SESSION via ``bind()`` — every run binds
    its own view before executing, never mutating a peer's binding.
    """

    def __init__(self, primary: DecisionEngine | None, fallback: DecisionEngine, *,
                 storage=None, run_id: str = "",
                 primary_only: frozenset[str] | None = None):
        self.primary = primary
        self.fallback = fallback
        self.storage = storage
        self.run_id = run_id
        # None = primary for every decision; else only these decision names
        self.primary_only = primary_only
        self.counts: dict[str, int] = {}
        self._local = threading.local()

    def bind(self, run_id: str) -> "DecisionRouter":
        """Per-run view: same engines, isolated run_id/counts (P0-4)."""
        import copy
        view = copy.copy(self)
        view.run_id = run_id
        view.counts = {}
        view._local = threading.local()
        return view

    def _use_primary(self, spec_name: str) -> bool:
        if self.primary is None:
            return False
        return self.primary_only is None or spec_name in self.primary_only

    async def _try(self, engine: DecisionEngine, spec: DecisionSpec, state: str,
                   subject_kind: str, subject_id: str) -> DecisionResult | None:
        try:
            return await engine.decide(spec, state, subject_kind=subject_kind,
                                       subject_id=subject_id)
        except Exception as exc:  # engine crash must not stop the run
            log.warning("decision engine %s failed on %s: %s", engine.name, spec.name, exc)
            return None

    async def _try_many(self, engine: DecisionEngine, specs: list[DecisionSpec],
                        state: str, subject_kind: str, subject_id: str,
                        ) -> list[DecisionResult] | None:
        try:
            batch = await engine.decide_many(specs, state, subject_kind=subject_kind,
                                             subject_id=subject_id)
        except Exception as exc:
            log.warning("decision engine %s batch failed: %s", engine.name, exc)
            return None
        if batch is None or len(batch) != len(specs):
            return None
        return batch

    async def decide(self, spec: DecisionSpec, state: str, *,
                     subject_kind: str = "", subject_id: str = "") -> DecisionResult:
        res: DecisionResult | None = None
        if self._use_primary(spec.name):
            res = await self._try(self.primary, spec, state, subject_kind, subject_id)
        if res is not None:
            self._record(res)
            return res
        return await self._fallback(spec, state, subject_kind, subject_id)

    async def decide_many(self, specs: list[DecisionSpec], state: str, *,
                          subject_kind: str = "", subject_id: str = "",
                          ) -> list[DecisionResult]:
        results: list[DecisionResult | None] = [None] * len(specs)
        if self.primary is not None:
            batch_specs = [s for s in specs if self._use_primary(s.name)]
            if batch_specs:
                batch = await self._try_many(self.primary, batch_specs, state,
                                             subject_kind, subject_id)
                if batch is not None:
                    by_name = {s.name: r for s, r in zip(batch_specs, batch)}
                    for i, s in enumerate(specs):
                        r = by_name.get(s.name)
                        if r is not None and not r.abstained:
                            results[i] = r
        for i, s in enumerate(specs):
            if results[i] is None:
                results[i] = await self._fallback(
                    s, state, subject_kind=subject_kind, subject_id=subject_id
                )
        out: list[DecisionResult] = []
        for r in results:
            assert r is not None
            self._record(r)
            out.append(r)
        return out

    def _record(self, res: DecisionResult) -> None:
        self.counts[res.engine] = self.counts.get(res.engine, 0) + 1
        if self.storage is not None and self.run_id:
            try:
                self.storage.add_decision(self.run_id, res.name, res)
            except Exception as exc:
                log.warning("failed to persist decision %s: %s", res.decision_id, exc)

    async def _fallback(self, spec: DecisionSpec, state: str, subject_kind: str,
                        subject_id: str) -> DecisionResult:
        """Fallback with last-resort refusal: a pipeline crash is worse than 'unknown'."""
        try:
            res = await self.fallback.decide(spec, state, subject_kind=subject_kind,
                                             subject_id=subject_id)
        except Exception as exc:
            log.error("fallback engine %s failed on %s: %s",
                      self.fallback.name, spec.name, exc)
            res = DecisionResult(
                name=spec.name, kind=spec.kind, engine=self.fallback.name,
                value=None, confidence=None, abstained=True, latency_ms=0.0,
                subject_kind=subject_kind, subject_id=subject_id,
                raw={"error": "fallback_failed"},
            )
        return res
