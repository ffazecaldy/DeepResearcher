"""Runtime budgets and on-disk caches (search results, fetched pages)."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


class LimitReached(RuntimeError):
    """A configured budget was exhausted."""


@dataclass
class RuntimeBudget:
    """Wall-clock and page budget for one run."""
    max_runtime_seconds: int
    max_total_pages: int
    _start: float = field(default_factory=time.monotonic)
    pages_fetched: int = 0
    limit_reason: str = ""

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def time_left(self) -> float:
        return self.max_runtime_seconds - self.elapsed()

    def check_time(self) -> None:
        if self.time_left() <= 0:
            self.limit_reason = (
                self.limit_reason or f"max_runtime_seconds={self.max_runtime_seconds} reached"
            )
            raise LimitReached(self.limit_reason)

    def consume_page(self) -> None:
        self.pages_fetched += 1
        if self.pages_fetched > self.max_total_pages:
            self.limit_reason = (
                self.limit_reason or f"max_total_pages={self.max_total_pages} reached"
            )
            raise LimitReached(self.limit_reason)


class DiskCache:
    """Namespaced sha256-keyed file cache."""

    def __init__(self, cache_dir: Path | str, enabled: bool = True):
        self.dir = Path(cache_dir)
        self.enabled = enabled
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(*parts: str) -> str:
        return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()

    def _path(self, ns: str, key: str, suffix: str):
        return self.dir / ns / f"{key}{suffix}"

    def get_bytes(self, ns: str, key: str) -> bytes | None:
        if not self.enabled:
            return None
        p = self._path(ns, key, ".bin")
        try:
            return p.read_bytes()
        except OSError:
            return None

    def set_bytes(self, ns: str, key: str, data: bytes) -> None:
        if not self.enabled:
            return
        p = self._path(ns, key, ".bin")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get_json(self, ns: str, key: str) -> object | None:
        if not self.enabled:
            return None
        p = self._path(ns, key, ".json")
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def set_json(self, ns: str, key: str, value: object) -> None:
        if not self.enabled:
            return
        p = self._path(ns, key, ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
