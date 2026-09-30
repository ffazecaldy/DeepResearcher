"""FastAPI application: run management, SSE event stream, exports, settings."""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from app.bootstrap import build_orchestrator
from app.config import Settings, load_settings
from app.events.bus import EventBus
from app.models import RunOutcome
from app.server.exporters import export_pdf
from app.server.schemas import (CancelResponse, DeleteResponse, RunStartRequest,
                                RunStartResponse, decision_specs_response,
                                detect_language)
from app.storage import Storage

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
_FINAL_EVENTS = {"run_completed", "run_failed", "run_cancelled"}


@dataclass
class RunHandle:
    cancel: asyncio.Event
    task: asyncio.Task | None = None
    outcome: RunOutcome | None = None
    started: float = field(default_factory=time.time)


def _sse(event_type: str, run_id: str, ts: float, cycle: int,
         payload: dict[str, Any], seq: int) -> str:
    data = json.dumps({"type": event_type, "run_id": run_id, "ts": ts,
                       "cycle": cycle, "seq": seq, "payload": payload},
                      ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    orch, comps = build_orchestrator(settings)
    storage: Storage = comps["storage"]
    bus: EventBus = comps["bus"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await comps["aclose"]()

    app = FastAPI(title="Deep Researcher", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://localhost:.*",
                       allow_methods=["*"], allow_headers=["*"])
    app.state.settings = settings
    app.state.storage = storage
    app.state.bus = bus
    app.state.orch = orch
    app.state.runs: dict[str, RunHandle] = {}

    # ---------- runs ----------
    @app.post("/api/runs", status_code=202)
    async def start_run(req: RunStartRequest) -> RunStartResponse:
        run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        handle = RunHandle(cancel=asyncio.Event())
        language = req.language or detect_language(req.question)

        async def _run() -> None:
            try:
                handle.outcome = await orch.run(req.question, language, req.depth,
                                                run_id, handle.cancel)
            except Exception as exc:  # keep the server alive on orchestrator crash
                log.exception("run %s crashed", run_id)
                handle.outcome = RunOutcome(run_id=run_id, status="failed",
                                            question=req.question, language=language,
                                            stats={"error": str(exc)})

        handle.task = asyncio.create_task(_run())
        app.state.runs[run_id] = handle
        return RunStartResponse(run_id=run_id)

    @app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        out = []
        for row in storage.list_runs():
            usage = storage.usage_for_run(row["id"]) or {}
            row["usage"] = {"llm_calls": usage.get("llm_calls", 0),
                            "llm_tokens_in": usage.get("llm_tokens_in", 0),
                            "llm_tokens_out": usage.get("llm_tokens_out", 0)}
            out.append(row)
        return out

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str) -> dict[str, Any]:
        run = storage.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} non trovato")
        report = storage.report_for_run(run_id)
        counts = {
            "sources": len(storage.sources_for_run(run_id)),
            "evidences": len(storage.evidences_for_run(run_id)),
            "claims": len(storage.claims_for_run(run_id)),
            "decisions": len(storage.decisions_for_run(run_id)),
            "events": len(storage.events_for_run(run_id)),
            "verifications": len(storage.verifications_for_run(run_id)),
        }
        return {"run": run,
                "report": ({"markdown": report["markdown"], "title": report["title"]}
                           if report else None),
                "counts": counts, "usage": storage.usage_for_run(run_id)}

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: str) -> DeleteResponse:
        handle = app.state.runs.get(run_id)
        if handle is not None:
            handle.cancel.set()
            if handle.task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(handle.task), timeout=10)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
            app.state.runs.pop(run_id, None)
        elif storage.get_run(run_id) is None:
            raise HTTPException(404, f"run {run_id} non trovato")
        storage.delete_run(run_id)
        return DeleteResponse()

    @app.delete("/api/runs")
    async def clear_runs() -> DeleteResponse:
        """Wipe the whole history (active runs are cancelled first)."""
        for handle in list(app.state.runs.values()):
            handle.cancel.set()
        await asyncio.sleep(0)  # let cancellation checkpoints observe the flag
        app.state.runs.clear()
        removed = storage.clear_all_runs()
        return DeleteResponse(deleted=bool(removed >= 0))

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> CancelResponse:
        handle = app.state.runs.get(run_id)
        if handle is not None:
            handle.cancel.set()
            return CancelResponse()
        if storage.get_run(run_id) is None:
            raise HTTPException(404, f"run {run_id} non trovato")
        return CancelResponse(cancelled=False)  # already finished

    # ---------- SSE ----------
    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        if storage.get_run(run_id) is None:
            raise HTTPException(404, f"run {run_id} non trovato")
        rows = storage.events_for_run(run_id)

        async def stream() -> AsyncIterator[str]:
            seq = 0
            yield _sse("replay_start", run_id, time.time(), 0,
                       {"count": len(rows)}, 0)
            handle = app.state.runs.get(run_id)
            queue = bus.subscribe(run_id) if handle is not None else None
            try:
                for row in rows:
                    seq += 1
                    yield _sse(row["type"], run_id, row["ts"], row["cycle"] or 0,
                               json.loads(row["payload_json"] or "{}"), seq)
                done = (handle is None or handle.outcome is not None
                        or (handle.task is not None and handle.task.done()))
                if not done and queue is not None:
                    while True:
                        if request.is_disconnected():
                            return
                        try:
                            ev = await asyncio.wait_for(queue.get(), timeout=15)
                        except asyncio.TimeoutError:
                            yield ": ping\n\n"
                            continue
                        seq += 1
                        yield _sse(ev.type, run_id, ev.ts, ev.cycle,
                                   ev.payload, seq)
                        if ev.type in _FINAL_EVENTS:
                            break
                # drain anything queued between replay and completion
                while queue is not None:
                    try:
                        ev = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    seq += 1
                    yield _sse(ev.type, run_id, ev.ts, ev.cycle, ev.payload, seq)
                seq += 1
                yield _sse("run_closed", run_id, time.time(), 0, {}, seq)
            finally:
                if queue is not None:
                    bus.unsubscribe(run_id, queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---------- exports ----------
    @app.get("/api/runs/{run_id}/report.md")
    async def report_md(run_id: str) -> PlainTextResponse:
        report = storage.report_for_run(run_id)
        if report is None:
            raise HTTPException(404, "report non presente per questo run")
        return PlainTextResponse(report["markdown"], media_type="text/markdown")

    @app.get("/api/runs/{run_id}/report.json")
    async def report_json(run_id: str) -> dict[str, Any]:
        report = storage.report_for_run(run_id)
        if report is None:
            raise HTTPException(404, "report non presente per questo run")
        from app.provenance.tracker import ProvenanceTracker
        return {
            "question": storage.get_run(run_id)["question"],
            "report_markdown": report["markdown"],
            "claims": storage.claims_for_run(run_id),
            "evidences": storage.evidences_for_run(run_id),
            "sources": storage.sources_for_run(run_id),
            "verifications": storage.verifications_for_run(run_id),
            "decisions": storage.decisions_for_run(run_id),
            "usage": storage.usage_for_run(run_id),
            "provenance": ProvenanceTracker(storage, run_id).export_graph(),
        }

    @app.get("/api/runs/{run_id}/report.pdf")
    async def report_pdf(run_id: str) -> FileResponse:
        report = storage.report_for_run(run_id)
        if report is None:
            raise HTTPException(404, "report non presente per questo run")
        out = Path(tempfile.gettempdir()) / f"dr_{run_id}.pdf"
        export_pdf(report["markdown"], out)
        return FileResponse(out, media_type="application/pdf",
                            filename=f"deep-researcher-{run_id}.pdf")

    # ---------- meta ----------
    @app.get("/api/decision-specs")
    async def decision_specs() -> dict[str, Any]:
        return decision_specs_response(settings)

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        try:
            import laya  # noqa: F401
            laya_available = True
        except ImportError:
            laya_available = False
        return {
            "llm_provider": settings.llm_provider.value,
            "llm_model": settings.llm_model,
            "llm_base_url": settings.llm_base_url,
            "search_provider": settings.search_provider.value,
            "decision_engine": settings.decision_engine.value,
            "decision_laya_decisions": settings.decision_laya_decisions,
            "max_cycles": settings.max_cycles,
            "max_concurrency": settings.max_concurrency,
            "laya_available": laya_available,
            "llm_api_key": "set" if settings.llm_api_key else "unset",
        }

    # ---------- static frontend ----------
    def _web(name: str, media: str) -> FileResponse:
        path = _WEB_DIR / name
        if not path.exists():
            raise HTTPException(404, f"file frontend mancante: web/{name}")
        return FileResponse(path, media_type=media,
                            headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def index() -> FileResponse:
        return _web("index.html", "text/html")

    @app.get("/app.js")
    async def app_js() -> FileResponse:
        return _web("app.js", "text/javascript")

    @app.get("/static/app.css")
    async def app_css() -> FileResponse:
        return _web("styles.css", "text/css")

    return app
