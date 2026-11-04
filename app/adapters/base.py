"""Provider-independent model adapter protocol."""

from collections.abc import AsyncIterator
from typing import Protocol

from app.schemas.chat import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse


class ModelAdapter(Protocol):
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Send a non-streaming chat completion request."""
        ...

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Return an async iterator of chat completion chunks."""
        ...

    async def healthcheck(self) -> bool:
        """Report whether the provider is reachable with valid credentials."""
        ...
