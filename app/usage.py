"""Normalize provider counters without exposing provider usage dictionaries."""

from pydantic import BaseModel, Field

from app.core.errors import GatewayError
from app.schemas.anthropic import AnthropicUsage
from app.schemas.chat import ChatCompletionUsage


class _InputDetails(BaseModel):
    cached_tokens: int | None = Field(default=None, ge=0, strict=True)


class _OutputDetails(BaseModel):
    reasoning_tokens: int | None = Field(default=None, ge=0, strict=True)


class _CompatibleUsage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_tokens_details: _InputDetails | None = None
    completion_tokens_details: _OutputDetails | None = None


def compatible_usage(data: object) -> ChatCompletionUsage | None:
    if data is None:
        return None
    raw = _CompatibleUsage.model_validate(data)
    usage = ChatCompletionUsage(input_tokens=raw.prompt_tokens, output_tokens=raw.completion_tokens)
    cached = raw.prompt_cache_hit_tokens
    if cached is None and raw.prompt_tokens_details is not None:
        cached = raw.prompt_tokens_details.cached_tokens
    if cached is not None and raw.prompt_tokens is not None and cached > raw.prompt_tokens:
        raise GatewayError(502, "UPSTREAM_INVALID_RESPONSE", "Invalid provider usage")
    reasoning = (
        raw.completion_tokens_details.reasoning_tokens if raw.completion_tokens_details else None
    )
    if (
        reasoning is not None
        and raw.completion_tokens is not None
        and reasoning > raw.completion_tokens
    ):
        raise GatewayError(502, "UPSTREAM_INVALID_RESPONSE", "Invalid provider usage")
    usage._details = {
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": reasoning,
    }
    return usage


def anthropic_usage(raw: AnthropicUsage) -> ChatCompletionUsage:
    usage = ChatCompletionUsage(
        input_tokens=raw.input_tokens
        + raw.cache_creation_input_tokens
        + raw.cache_read_input_tokens,
        output_tokens=raw.output_tokens,
    )
    usage._details = {
        "cache_read_input_tokens": raw.cache_read_input_tokens,
        "cache_creation_input_tokens": raw.cache_creation_input_tokens,
        "reasoning_output_tokens": None,
    }
    return usage


def aggregate_usage(usages: list[ChatCompletionUsage | None]) -> ChatCompletionUsage | None:
    if not usages or all(usage is None for usage in usages):
        return None
    values = {}
    for name in ("input_tokens", "output_tokens"):
        counters = [getattr(usage, name) if usage is not None else None for usage in usages]
        values[name] = sum(counters) if all(value is not None for value in counters) else None
    return ChatCompletionUsage(**values)
