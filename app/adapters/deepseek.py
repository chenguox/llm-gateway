"""DeepSeek Chat Completions adapter."""

from typing import Self

import httpx2 as httpx

from app.adapters.compatible import ChatCompletionsAdapter
from app.core.config import Settings


class DeepSeekChatAdapter(ChatCompletionsAdapter):
    """DeepSeek defaults over the shared Chat Completions HTTP protocol."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout, client=client)

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
