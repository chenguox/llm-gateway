"""Provider-independent model adapter protocol."""

from typing import Protocol

from app.schemas.chat import LLMRequest, LLMResponse
from app.streaming.provider import CompletionStream


class ModelAdapter(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse | CompletionStream:
        """Return a completion or an open event stream according to request.stream."""
        ...

    async def healthcheck(self) -> bool:
        """Report whether the provider is reachable with valid credentials."""
        ...

    async def aclose(self) -> None:
        """Release resources owned by this adapter."""
        ...
