"""Ollama chat client."""
from __future__ import annotations

from typing import Any

import httpx

from app.clients.llm.base import (
    DEFAULT_USER_AGENT,
    LLMApiError,
    LLMClient,
    estimate_tokens,
    post_json,
)
from app.config import Settings


class OllamaClient(LLMClient):
    """Client for the Ollama ``/api/chat`` endpoint."""

    def __init__(self, settings: Settings, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = "ollama"
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.llm_timeout_s,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            transport=transport,
        )

    async def complete(self, system: str, user: str, *, max_tokens: int | None = None,
                       temperature: float | None = None, force_json: bool = False) -> str:
        """Call /api/chat; ``force_json`` sets the top-level ``format: json``."""
        s = self._settings
        payload: dict[str, Any] = {
            "model": s.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else s.llm_temperature,
                "num_predict": max_tokens if max_tokens is not None else s.llm_max_tokens,
            },
        }
        if force_json:
            payload["format"] = "json"
        url = f"{s.llm_base_url.rstrip('/')}/api/chat"
        data = await post_json(self._client, url, headers={}, payload=payload)
        try:
            text: str = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMApiError("unexpected Ollama response shape") from exc
        # Ollama reports token counts as top-level prompt_eval_count / eval_count.
        self._record(
            int(data.get("prompt_eval_count", estimate_tokens(system + user))),
            int(data.get("eval_count", estimate_tokens(text))),
        )
        return text

    async def aclose(self) -> None:
        await self._client.aclose()
