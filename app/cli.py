"""CLI entrypoint: python -m app.cli "question" [--depth standard] [...]"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
import uuid
from pathlib import Path

from app.config import Depth, load_settings
from app.logging_setup import configure_logging


def _detect_language(question: str) -> str:
    italian_marks = ("à", "è", "é", "ì", "ò", "ù", "perché", "come ", "qual ",
                     "quali", "chi ", "cos'è", "storia ")
    low = question.lower()
    return "it" if any(m in low for m in italian_marks) else "en"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="deep-researcher",
        description="Autonomous research agent with verifiable citations.")
    p.add_argument("question", help="the research question")
    p.add_argument("--depth", choices=[d.value for d in Depth],
                   default=Depth.STANDARD.value)
    p.add_argument("--language", default=None,
                   help="report language (default: auto-detect it/en)")
    p.add_argument("--db", default=None, help="SQLite path override")
    p.add_argument("--md-out", default=None, help="copy the final markdown here")
    p.add_argument("--json-out", default=None, help="copy the provenance JSON here")
    p.add_argument("--no-laya", action="store_true",
                   help="force the LLM decision engine (skip laya)")
    p.add_argument("--max-cycles", type=int, default=None)
    p.add_argument("--quiet", action="store_true", help="suppress event lines")
    return p.parse_args(argv)


async def _pump_events(bus, run_id: str, quiet: bool) -> None:
    q = bus.subscribe(run_id)
    t0 = time.time()
    try:
        while True:
            ev = await q.get()
            if not quiet:
                ts = time.strftime("%H:%M:%S")
                extras = "".join(f" {k}={str(v)[:60]}"
                                 for k, v in list(ev.payload.items())[:2])
                print(f"[{ts}] cycle={ev.cycle} {ev.type.upper()}{extras}",
                      file=sys.stderr)
            if ev.type in ("run_completed", "run_failed", "run_cancelled"):
                return
            if time.time() - t0 > 3600:
                return
    finally:
        bus.unsubscribe(run_id, q)


async def _amain(args: argparse.Namespace, cancel: asyncio.Event):
    from app.bootstrap import build_orchestrator

    settings = load_settings()
    if args.db:
        settings.db_path = Path(args.db)
    if args.max_cycles:
        settings.max_cycles = args.max_cycles
    if args.no_laya:
        from app.config import DecisionEngineChoice
        settings.decision_engine = DecisionEngineChoice.LLM
    language = args.language or _detect_language(args.question)
    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    orch, comps = build_orchestrator(settings)
    watcher = asyncio.create_task(_pump_events(comps["bus"], run_id, args.quiet))
    try:
        outcome = await orch.run(args.question, language, Depth(args.depth),
                                 run_id, cancel)
    finally:
        cancel.set()
        await watcher
        await comps["aclose"]()
    return outcome


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    configure_logging("WARNING" if args.quiet else "INFO")
    cancel = asyncio.Event()
    try:
        outcome = asyncio.run(_amain(args, cancel))
    except KeyboardInterrupt:
        cancel.set()
        print("\n[interrupt] richiesta di stop inviata, chiusura…", file=sys.stderr)
        return 130
    except Exception as exc:  # config/build errors: clean message, no traceback
        print(f"errore: {exc}", file=sys.stderr)
        return 1

    print(f"\nrun: {outcome.run_id}  status: {outcome.status}")
    if outcome.limit_reached:
        print(f"limite: {outcome.limit_note}")
    if outcome.markdown_path:
        md = Path(outcome.markdown_path).read_text(encoding="utf-8")
        print(md)
        if args.md_out:
            Path(args.md_out).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(outcome.markdown_path, args.md_out)
        if args.json_out and outcome.json_path:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(outcome.json_path, args.json_out)
    else:
        print("nessun report prodotto (nessuna evidenza raccolta).", file=sys.stderr)
        return 0 if outcome.status in ("completed", "cancelled") else 1
    return 0 if outcome.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
