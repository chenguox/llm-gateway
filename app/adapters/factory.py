"""Construct a provider adapter from a validated model configuration."""

from pydantic import SecretStr

from app.adapters.base import ModelAdapter
from app.adapters.compatible import ChatCompletionsAdapter
from app.adapters.deepseek import DeepSeekChatAdapter
from app.schemas.models import ModelConfiguration


def create_adapter(configuration: ModelConfiguration, api_key: SecretStr) -> ModelAdapter:
    adapter_class = {
        "deepseek": DeepSeekChatAdapter,
        "openai_compatible": ChatCompletionsAdapter,
    }[configuration.provider]
    return adapter_class(
        api_key=api_key.get_secret_value(),
        base_url=str(configuration.base_url),
        timeout=configuration.timeout_seconds,
    )
