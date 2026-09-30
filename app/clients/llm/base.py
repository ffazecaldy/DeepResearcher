"""LLM client abstraction: shared HTTP/retry helper, base class and factory."""
from __future__ import annotations

import abc
import asyncio
from typing import Any

import httpx

from app.config import LLMProvider, Settings

DEFAULT_USER_AGENT = "DeepResearcher/0.1 (local research agent)"
_RETRY_WAIT_S = 2.0


class LLMApiError(RuntimeError):
    """LLM API call failed (after retry) or returned an unusable response."""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token) when usage is not reported."""
    return len(text) // 4


class LLMClient(abc.ABC):
    """Base class for LLM backends; tracks call and token counters."""

    name: str = "llm"
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    @abc.abstractmethod
    async def complete(self, system: str, user: str, *, max_tokens: int | None = None,
                       temperature: float | None = None, force_json: bool = False) -> str:
        """Return the model completion for one system+user exchange."""

    async def aclose(self) -> None:
        """Release underlying HTTP resources."""

    def _record(self, tokens_in: int, tokens_out: int) -> None:
        """Accumulate usage counters for one completed call."""
        self.llm_calls += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out


def _is_retryable(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


async def post_json(client: httpx.AsyncClient, url: str, *,
                    headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    """POST ``payload`` as JSON with a single retry on 429/5xx/timeout.

    Error messages never include credentials or request bodies.
    """
    last_error = ""
    for attempt in (1, 2):
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            last_error = f"timeout calling {url}"
        except httpx.HTTPError as exc:
            last_error = f"HTTP error calling {url}: {type(exc).__name__}: {exc}"
        else:
            status = resp.status_code
            if _is_retryable(status):
                if attempt == 1:
                    last_error = f"HTTP {status} from {url}"
                    await asyncio.sleep(_RETRY_WAIT_S)
                    continue
                raise LLMApiError(f"HTTP {status} from {url} after retry")
            if status in (401, 403):
                # Never include credentials in the error message.
                raise LLMApiError(
                    f"authentication failed (HTTP {status}) for {url}; check the API key"
                )
            if resp.is_error:
                raise LLMApiError(f"HTTP {status} from {url}: {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise LLMApiError(f"invalid JSON response from {url}") from exc
        if attempt == 1:
            await asyncio.sleep(_RETRY_WAIT_S)
    raise LLMApiError(last_error or f"request to {url} failed after retry")


def build_llm_client(settings: Settings) -> LLMClient:
    """Build the LLM client selected by ``settings.llm_provider``."""
    if settings.llm_provider is LLMProvider.ANTHROPIC:
        from app.clients.llm.anthropic import AnthropicClient

        return AnthropicClient(settings)
    if settings.llm_provider is LLMProvider.OPENAI_COMPAT:
        from app.clients.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(settings)
    if settings.llm_provider is LLMProvider.OLLAMA:
        from app.clients.llm.ollama import OllamaClient

        return OllamaClient(settings)
    raise LLMApiError(f"unsupported LLM provider: {settings.llm_provider!r}")
