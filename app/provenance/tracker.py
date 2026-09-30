"""Reads a run from storage and exports its provenance graph."""
from __future__ import annotations

import json
from typing import Any


class ProvenanceTracker:
    """Aggregates all persisted run elements into an auditable graph."""

    def __init__(self, storage: Any, run_id: str):
        self.storage = storage
        self.run_id = run_id

    def export_graph(self) -> dict[str, list[dict[str, str]]]:
        from app.provenance.graph import build_graph

        run = self.storage.get_run(self.run_id) or {"id": self.run_id, "question": ""}
        chunks: list[dict[str, Any]] = []
        for src in self.storage.sources_for_run(self.run_id):
            chunks.extend(self.storage.chunks_for_source(src["id"]))
        return build_graph(
            run,
            self.storage.subquestions_for_run(self.run_id),
            self.storage.queries_for_run(self.run_id),
            self.storage.search_results_for_run(self.run_id),
            self.storage.sources_for_run(self.run_id),
            chunks,
            self.storage.evidences_for_run(self.run_id),
            self.storage.claims_for_run(self.run_id),
            self.storage.verifications_for_run(self.run_id),
            self.storage.decisions_for_run(self.run_id),
        )

    def export_json(self) -> str:
        return json.dumps(self.export_graph(), ensure_ascii=False)
