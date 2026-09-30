"""FakeLLMClient: scriptable LLM double for unit tests."""
from __future__ import annotations

from collections import deque


class FakeLLMClient:
    """Returns scripted responses in order, or calls a callable.

    Records every (system, user, kwargs) and counts calls.
    """

    def __init__(self, responses: list[str] | callable):
        if callable(responses):
            self._fn = responses
            self._queue: deque[str] | None = None
        else:
            self._fn = None
            self._queue = deque(responses)
        self.calls: list[dict] = []  # instance-level: no leakage across tests

    async def complete(self, system: str, user: str, *,
                       max_tokens=None, temperature=None,
                       force_json: bool = False) -> str:
        self.calls.append({
            "system": system,
            "user": user,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "force_json": force_json,
        })
        if self._fn is not None:
            return self._fn(system=system, user=user, force_json=force_json)
        if not self._queue:
            raise AssertionError("FakeLLMClient: no more scripted responses")
        return self._queue.popleft()

    # -- inspection helpers -------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_system(self) -> str:
        return self.calls[-1]["system"]

    @property
    def last_user(self) -> str:
        return self.calls[-1]["user"]
