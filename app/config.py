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
    Depth.APPROFONDITA: 5,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DR_", env_file=".env", extra="ignore")

    # LLM
    llm_provider: LLMProvider = LLMProvider.OLLAMA
    llm_model: str = "llama3.1"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    llm_timeout_s: float = 120.0
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.2

    # Search
    search_provider: SearchProvider = SearchProvider.TAVILY
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

    # Limits
    max_cycles: int = 3
    max_queries_per_cycle: int = 6
    max_pages_per_query: int = 5
    max_total_pages: int = 30
    max_runtime_seconds: int = 600
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
    db_path: Path = Path("data/deep_researcher.db")
    cache_enabled: bool = True
    cache_dir: Path = Path("data/cache")

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
