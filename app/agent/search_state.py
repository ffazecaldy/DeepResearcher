"""Shared research state: queries done, URLs seen, domain counts, attempt counts.

Passed to every cycle so the pipeline can diversify queries and never re-read
the same page, and so termination has memory (no infinite loops).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.security import normalize_for_dedupe


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# B4: ruoli del ciclo — esplorazione (1), approfondimento (2), verifica (3),
# poi i cicli extra dell'approfondita (4+)
CYCLE_ROLES: dict[int, str] = {
    1: "esplorazione",
    2: "approfondimento",
    3: "verifica",
    4: "nuovi_sottotemi",
    5: "verifica_ulteriore",
    6: "approfondimento_2",
    7: "sintesi_cross",
    8: "verifica_finale",
}


def cycle_role(cycle: int) -> str:
    """Ruolo dichiarato del ciclo (payload di cycle_started)."""
    return CYCLE_ROLES.get(max(1, cycle), "verifica")


def novelty_score(url: str, state: "SearchState") -> int:
    """Scala di novità: dominio nuovo=2, URL nuovo su dominio già visto=1,
    URL già visto=0. Deterministica, zero LLM."""
    key = state.norm_url(url)
    if key in state.seen_urls:
        return 0
    return 2 if state.domain_count.get(domain_of(url), 0) == 0 else 1


@dataclass
class SearchState:
    """Everything the pipeline remembers across cycles."""
    executed_queries: list[str] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)      # normalized
    domain_count: dict[str, int] = field(default_factory=dict)
    attempts_by_sq: dict[str, int] = field(default_factory=dict)
    evidence_count: int = 0

    def norm_url(self, url: str) -> str:
        return normalize_for_dedupe(url)

    def see_query(self, text: str) -> None:
        self.executed_queries.append(self.norm_query(text))

    def already_ran(self, text: str) -> bool:
        return self.norm_query(text) in set(self.executed_queries)

    @staticmethod
    def norm_query(text: str) -> str:
        return " ".join(text.lower().split())

    def add_url(self, url: str) -> bool:
        """Register a URL; returns True if it was NEW."""
        key = self.norm_url(url)
        if key in self.seen_urls:
            return False
        self.seen_urls.add(key)
        self.domain_count[domain_of(url)] = self.domain_count.get(domain_of(url), 0) + 1
        return True


def token_set(text: str) -> set[str]:
    import re
    return set(re.findall(r"[\w']+", text.lower(), re.UNICODE))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def too_similar(candidate: str, executed: list[str], threshold: float = 0.6) -> bool:
    """True when candidate overlaps too much with any already-executed query."""
    cand = token_set(candidate)
    return any(jaccard(cand, token_set(q)) >= threshold for q in executed)
