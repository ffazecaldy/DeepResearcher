"""Anthropic Messages API client."""
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

_API_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicClient(LLMClient):
    """Client for the Anthropic ``/v1/messages`` endpoint."""

    def __init__(self, settings: Settings, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = "anthropic"
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.llm_timeout_s,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            transport=transport,
        )

    async def complete(self, system: str, user: str, *, max_tokens: int | None = None,
                       temperature: float | None = None, force_json: bool = False) -> str:
        """Call /v1/messages; ``force_json`` is a no-op (JSON is prompt-driven)."""
        s = self._settings
        headers = {
            "x-api-key": s.llm_api_key,
            "anthropic-version": _API_VERSION,
        }
        payload: dict[str, Any] = {
            "model": s.llm_model,
            "max_tokens": max_tokens if max_tokens is not None else s.llm_max_tokens,
            "temperature": temperature if temperature is not None else s.llm_temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        url = f"{_DEFAULT_BASE_URL}/v1/messages"
        data = await post_json(self._client, url, headers=headers, payload=payload)
        try:
            text: str = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMApiError("unexpected Anthropic response shape") from exc
        usage = data.get("usage") or {}
        self._record(
            int(usage.get("input_tokens", estimate_tokens(system + user))),
            int(usage.get("output_tokens", estimate_tokens(text))),
        )
        return text

    async def aclose(self) -> None:
        await self._client.aclose()
