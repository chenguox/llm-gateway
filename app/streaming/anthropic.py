"""Normalize Messages events, including reasoning and cumulative cached usage."""

import json
from collections.abc import AsyncGenerator
from typing import Annotated, Literal

import httpx2 as httpx
from pydantic import BaseModel, Field, TypeAdapter

from app.core.errors import GatewayError
from app.schemas.anthropic import AnthropicMessageResponse, ContentBlock
from app.schemas.chat import ChatCompletionUsage
from app.schemas.streaming import StreamDelta, StreamFinishReason, StreamUpdate
from app.streaming.provider import invalid_stream, read_sse


def finish_reason(reason: str) -> str:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "model_context_window_exceeded": "length",
        "refusal": "content_filter",
    }.get(reason, reason)


class _Start(BaseModel):
    message: AnthropicMessageResponse


class _BlockStart(BaseModel):
    index: int = Field(ge=0, strict=True)
    content_block: ContentBlock


class _TextDelta(BaseModel):
    type: Literal["text_delta"]
    text: str


class _ThinkingDelta(BaseModel):
    type: Literal["thinking_delta"]
    thinking: str


class _SignatureDelta(BaseModel):
    type: Literal["signature_delta"]
    signature: str


class _BlockDelta(BaseModel):
    index: int = Field(ge=0, strict=True)
    delta: Annotated[_TextDelta | _ThinkingDelta | _SignatureDelta, Field(discriminator="type")]


class _BlockStop(BaseModel):
    index: int = Field(ge=0, strict=True)


class _Usage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_read_input_tokens: int | None = Field(default=None, ge=0, strict=True)


class _MessageDelta(BaseModel):
    stop_reason: str | None = None


class _MessageUpdate(BaseModel):
    delta: _MessageDelta
    usage: _Usage | None = None


_object = TypeAdapter(dict[str, object])


def _usage(values: dict[str, int]) -> ChatCompletionUsage:
    prompt = (
        values["input_tokens"]
        + values["cache_creation_input_tokens"]
        + values["cache_read_input_tokens"]
    )
    completion = values["output_tokens"]
    return ChatCompletionUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


async def normalize_messages(response: httpx.Response) -> AsyncGenerator[StreamUpdate]:
    started = False
    stopped: set[int] = set()
    blocks: dict[int, str] = {}
    values: dict[str, int] = {}
    reason = None
    async for record in read_sse(response):
        data = _object.validate_python(json.loads(record.data))
        kind = data.get("type")
        if record.event != "message" and record.event != kind:
            raise invalid_stream()
        if kind == "error":
            raise GatewayError(502, "UPSTREAM_STREAM_ERROR", "Model provider stream failed")
        if kind == "ping":
            continue
        if kind == "message_start":
            if started:
                raise invalid_stream()
            message = _Start.model_validate(data).message
            if message.content or message.stop_reason is not None:
                raise invalid_stream()
            started = True
            values = message.usage.model_dump()
            yield StreamUpdate(type="delta", delta=StreamDelta(index=0, role="assistant"))
            yield StreamUpdate(type="usage", usage=_usage(values))
        elif kind == "content_block_start":
            block = _BlockStart.model_validate(data)
            if not started or reason is not None or block.index in blocks or block.index in stopped:
                raise invalid_stream()
            blocks[block.index] = block.content_block.type
            if block.content_block.type == "text" and block.content_block.text:
                yield StreamUpdate(
                    type="delta", delta=StreamDelta(index=0, content=block.content_block.text)
                )
            elif block.content_block.type == "thinking" and block.content_block.thinking:
                yield StreamUpdate(
                    type="delta",
                    delta=StreamDelta(index=0, reasoning_content=block.content_block.thinking),
                )
        elif kind == "content_block_delta":
            block = _BlockDelta.model_validate(data)
            if not started or reason is not None or block.index not in blocks:
                raise invalid_stream()
            delta = block.delta
            if delta.type == "text_delta" and blocks[block.index] == "text":
                yield StreamUpdate(type="delta", delta=StreamDelta(index=0, content=delta.text))
            elif delta.type == "thinking_delta" and blocks[block.index] == "thinking":
                yield StreamUpdate(
                    type="delta", delta=StreamDelta(index=0, reasoning_content=delta.thinking)
                )
            elif delta.type != "signature_delta" or blocks[block.index] != "thinking":
                raise invalid_stream()
        elif kind == "content_block_stop":
            index = _BlockStop.model_validate(data).index
            if not started or index not in blocks:
                raise invalid_stream()
            blocks.pop(index)
            stopped.add(index)
        elif kind == "message_delta":
            if not started or blocks:
                raise invalid_stream()
            update = _MessageUpdate.model_validate(data)
            if update.delta.stop_reason is not None:
                if reason is not None:
                    raise invalid_stream()
                reason = finish_reason(update.delta.stop_reason)
            if update.usage is not None:
                values.update(update.usage.model_dump(exclude_none=True))
                yield StreamUpdate(type="usage", usage=_usage(values))
        elif kind == "message_stop":
            if not started or blocks or reason is None:
                raise invalid_stream()
            yield StreamUpdate(
                type="completed",
                usage=_usage(values),
                finish_reasons=[StreamFinishReason(index=0, finish_reason=reason)],
            )
            return
        elif not isinstance(kind, str):
            raise invalid_stream()
    raise invalid_stream()
