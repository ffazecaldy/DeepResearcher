"""Wires all components together; the single composition root of the app."""
from __future__ import annotations

from app.config import Settings, load_settings
from app.events.bus import EventBus
from app.limits import DiskCache

def build_orchestrator(settings: Settings | None = None, bus: EventBus | None = None, *,
                       storage=None):
    """Create the full pipeline; returns (orchestrator, components).

    components carries the objects owning network resources (close them via
    ``await components['aclose']()`` when done).
    """
    settings = settings or load_settings()

    from app.storage import Storage
    storage = storage or Storage(settings.db_path)
    bus = bus or EventBus(persist=storage.add_event)

    from app.clients.llm.base import build_llm_client
    from app.clients.search.base import build_search_client
    llm = build_llm_client(settings)
    # B4: provider multipli -> MultiProviderSearchClient in cascata
    from app.clients.search.multi import build_search_clients
    _clients = build_search_clients(settings)
    if len(_clients) == 1:
        search = _clients[0]
    else:
        from app.clients.search.multi import MultiProviderSearchClient
        search = MultiProviderSearchClient(_clients)

    from app.agent.fetcher import Fetcher
    from app.agent.searcher import Searcher
    cache = DiskCache(settings.cache_dir, settings.cache_enabled,
                      ttl_s=getattr(settings, "cache_ttl_s", 0))
    fetcher = Fetcher(settings, cache)
    searcher = Searcher(search, cache, settings)

    from app.agent.gap_checker import GapChecker
    from app.agent.planner import Planner
    from app.agent.reader import Reader
    from app.agent.verifier import Verifier
    from app.agent.writer import Writer
    planner = Planner(llm)
    reader = Reader(llm)
    gap_checker = GapChecker(llm)
    writer = Writer(llm)
    verifier = Verifier(llm, max_concurrency=settings.llm_max_concurrency)

    from app.decision.base import DecisionRouter
    from app.decision.llm_fallback import LLMDecisionEngine
    primary = None
    if settings.decision_engine.value == "laya":
        try:
            from app.decision.laya import LayaDecisionEngine
            # defensive: an inline-comment value from .env must not reach laya
            laya_model = (settings.laya_model or "").strip()
            if laya_model.startswith("#"):
                laya_model = ""
            primary = LayaDecisionEngine(
                min_confidence=settings.laya_min_confidence,
                model=laya_model, max_len=settings.laya_max_len)
        except Exception as exc:  # never block startup on the decision engine
            import logging
            logging.getLogger(__name__).warning("laya engine unavailable: %s", exc)
    fallback = LLMDecisionEngine(llm)
    primary_only = None
    if settings.decision_engine.value == "laya":
        names = [n.strip() for n in settings.decision_laya_decisions.split(",")
                 if n.strip()]
        primary_only = frozenset(names)  # empty -> laya never used
    decisions = DecisionRouter(primary, fallback, storage=storage,
                               primary_only=primary_only)

    from app.agent.orchestrator import Orchestrator
    orch = Orchestrator(settings, storage, bus, llm, searcher, fetcher,
                        decisions, planner, reader, gap_checker, writer, verifier)

    async def aclose() -> None:
        await fetcher.aclose()
        await search.aclose()
        await llm.aclose()

    return orch, {"storage": storage, "bus": bus, "llm": llm, "search": search,
                  "fetcher": fetcher, "cache": cache, "decisions": decisions,
                  "aclose": aclose}
