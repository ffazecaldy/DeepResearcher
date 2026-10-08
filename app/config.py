"""Application configuration, loaded from DR_* environment variables / .env."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPAT = "openai_compat"
    OLLAMA = "ollama"


class SearchProvider(StrEnum):
    TAVILY = "tavily"
    BRAVE = "brave"
    SEARXNG = "searxng"
    DUCKDUCKGO = "duckduckgo"


class DecisionEngineChoice(StrEnum):
    LAYA = "laya"
    LLM = "llm"


class Depth(StrEnum):
    RAPIDA = "rapida"
    STANDARD = "standard"
    APPROFONDITA = "approfondita"


DEPTH_CYCLES: dict[Depth, int] = {
    Depth.RAPIDA: 1,
    Depth.STANDARD: 3,
    Depth.APPROFONDITA: 8,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DR_", env_file=".env", extra="ignore")

    # LLM
    llm_provider: LLMProvider = LLMProvider.OLLAMA
    llm_model: str = "llama3.1"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    llm_timeout_s: float = 120.0
    llm_max_tokens: int = 8192
    llm_temperature: float = 0.2
    llm_max_concurrency: int = 3  # max parallel LLM calls (provider 429 protection)

    # Search
    search_provider: SearchProvider = SearchProvider.TAVILY
    # B4: provider extra in cascata (CSV, es. "duckduckgo,tavily"); vuoto = solo primario
    search_providers: str = ""
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    brave_api_key: str = ""
    brave_base_url: str = "https://api.search.brave.com/res/v1"
    searxng_base_url: str = ""

    # Fetch
    fetch_timeout_s: float = 20.0
    fetch_user_agent: str = "DeepResearcher/0.1 (local research agent)"
    fetch_max_redirects: int = 5
    per_domain_min_interval_s: float = 1.0
    respect_robots: bool = True

    # Reader depth
    reader_chunks_per_source: int = 12  # chunks read per accepted source (depth)
    usage_emit_every_s: float = 2.0  # throttle for usage_update events

    # Limits
    # default = DEPTH_CYCLES max: cycle_budget fa min(max_cycles, DEPTH_CYCLES[depth]),
    # quindi max_cycles < 8 troncherebbe l'approfondita
    max_cycles: int = 8
    max_queries_per_cycle: int = 6
    max_pages_per_query: int = 5
    max_pages_per_domain: int = 2  # per-cycle domain cap (0 = unlimited)
    max_total_pages: int = 30
    max_runtime_seconds: int = 600
    # B5: budget tempo PER CICLO (0 = disattivo); fermare cicli stantii senza
    # uccidere il budget complessivo del run
    max_seconds_per_cycle: int = 0
    max_concurrency: int = 8
    reader_max_doc_chars: int = 16000

    # Decision engine
    decision_engine: DecisionEngineChoice = DecisionEngineChoice.LAYA
    # decision names routed to laya when decision_engine=laya (comma-separated;
    # benchmark Fase 2: is_relevant 85% @85ms, choice/score zero-shot deboli)
    decision_laya_decisions: str = "is_relevant"
    laya_min_confidence: float = 0.35
    laya_model: str = ""  # empty = Router auto-routing
    laya_max_len: int = 0  # 0 = model default

    # Storage / cache
    report_language: str = "it"  # REPORT_LANGUAGE: global output language
    db_path: Path = Path("data/deep_researcher.db")
    cache_enabled: bool = True
    cache_dir: Path = Path("data/cache")
    cache_ttl_s: int = 86400  # P2: TTL entries JSON cache (0 = no expiry)

    # Comma-separated domain blocklist
    domain_blocklist: str = ""

    def cycle_budget(self, depth: Depth) -> int:
        """Effective number of research cycles for a depth preset."""
        return max(1, min(self.max_cycles, DEPTH_CYCLES[depth]))

    def blocked_domains(self) -> frozenset[str]:
        return frozenset(
            d.strip().lower() for d in self.domain_blocklist.split(",") if d.strip()
        )


def load_settings() -> Settings:
    """Load settings from environment / .env."""
    return Settings()
