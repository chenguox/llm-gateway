"""Provider-independent model adapter protocol."""

from collections.abc import AsyncIterator
from typing import Protocol

from app.schemas.chat import ChatCompletionChunk, LLMRequest, LLMResponse


class ModelAdapter(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a non-streaming chat completion request."""
        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Return an async iterator of chat completion chunks."""
        ...

    async def healthcheck(self) -> bool:
        """Report whether the provider is reachable with valid credentials."""
        ...

    async def aclose(self) -> None:
        """Release resources owned by this adapter."""
        ...
