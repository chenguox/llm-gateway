"""DeepSeek Chat Completions adapter."""

from collections.abc import AsyncIterator
from types import TracebackType
from typing import Self

import httpx2 as httpx

from app.core.config import Settings
from app.schemas.chat import ChatCompletionChunk, LLMRequest, LLMResponse


class DeepSeekChatAdapter:
    """Call DeepSeek through an async, optionally caller-owned HTTP client."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key must not be empty")
        if timeout <= 0:
            raise ValueError("DeepSeek timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key.strip()}"}
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    @classmethod
    def from_settings(cls, settings: Settings, *, client: httpx.AsyncClient | None = None) -> Self:
        if settings.deepseek_api_key is None:
            raise ValueError("DEEPSEEK_API_KEY is required to use DeepSeek")
        return cls(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=str(settings.deepseek_base_url),
            timeout=settings.deepseek_timeout_seconds,
            client=client,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one request and validate the upstream completion response."""
        payload = request.model_dump(mode="json", exclude_none=True)

        payload["stream"] = False
        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return LLMResponse.model_validate(response.json())

    def stream(self, request: LLMRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Reserved for future SSE support."""
        raise NotImplementedError("DeepSeek streaming is not implemented yet")

    async def healthcheck(self) -> bool:
        """Check authenticated access to the models endpoint without generating tokens."""
        try:
            response = await self._client.get(
                f"{self._base_url}/models",
                headers=self._headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def aclose(self) -> None:
        """Close an owned client; injected clients remain the caller's responsibility."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
