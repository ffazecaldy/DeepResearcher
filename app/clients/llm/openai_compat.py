"""OpenAI-compatible chat-completions client."""
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


class OpenAICompatClient(LLMClient):
    """Client for any OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(self, settings: Settings, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = "openai_compat"
        self._settings = settings
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._client = httpx.AsyncClient(
            timeout=settings.llm_timeout_s,
            headers=headers,
            transport=transport,
        )

    async def complete(self, system: str, user: str, *, max_tokens: int | None = None,
                       temperature: float | None = None, force_json: bool = False) -> str:
        """Call /chat/completions; ``force_json`` sets response_format json_object."""
        s = self._settings
        payload: dict[str, Any] = {
            "model": s.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature if temperature is not None else s.llm_temperature,
            "max_tokens": max_tokens if max_tokens is not None else s.llm_max_tokens,
        }
        if force_json:
            payload["response_format"] = {"type": "json_object"}
        url = f"{s.llm_base_url.rstrip('/')}/chat/completions"
        data = await post_json(self._client, url, headers={}, payload=payload)
        try:
            text: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMApiError("unexpected OpenAI-compatible response shape") from exc
        usage = data.get("usage") or {}
        self._record(
            int(usage.get("prompt_tokens", estimate_tokens(system + user))),
            int(usage.get("completion_tokens", estimate_tokens(text))),
        )
        return text

    async def aclose(self) -> None:
        await self._client.aclose()
