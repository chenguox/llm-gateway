"""Normalize Chat Completions SSE records without exposing provider envelopes."""

import json
from collections.abc import AsyncGenerator
from typing import Literal

import httpx2 as httpx
from pydantic import BaseModel, Field

from app.core.errors import GatewayError
from app.schemas.chat import ChatCompletionUsage
from app.schemas.streaming import StreamDelta, StreamFinishReason, StreamUpdate
from app.streaming.provider import invalid_stream, read_sse


class _Delta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list | None = None
    function_call: dict | None = None
    refusal: str | None = None


class _Choice(BaseModel):
    index: int = Field(ge=0, strict=True)
    delta: _Delta
    finish_reason: str | None = None


class _Frame(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"]
    created: int
    model: str
    choices: list[_Choice]
    usage: ChatCompletionUsage | None = None


async def normalize_completions(response: httpx.Response) -> AsyncGenerator[StreamUpdate]:
    seen: set[int] = set()
    finished: dict[int, str] = {}
    usage = None
    async for record in read_sse(response):
        if record.data == "[DONE]":
            if not seen or seen != finished.keys():
                raise invalid_stream()
            yield StreamUpdate(
                type="completed",
                usage=usage,
                finish_reasons=[
                    StreamFinishReason(index=index, finish_reason=finished[index])
                    for index in sorted(finished)
                ],
            )
            return
        data = json.loads(record.data)
        if record.event == "error" or isinstance(data, dict) and "error" in data:
            raise GatewayError(502, "UPSTREAM_STREAM_ERROR", "Model provider stream failed")
        frame = _Frame.model_validate(data)
        indices: set[int] = set()
        for choice in frame.choices:
            if choice.index in indices or choice.index in finished:
                raise invalid_stream()
            indices.add(choice.index)
            seen.add(choice.index)
            delta = choice.delta
            if delta.tool_calls or delta.function_call or delta.refusal:
                raise invalid_stream()
            if (
                delta.role is not None
                or delta.content is not None
                or delta.reasoning_content is not None
            ):
                yield StreamUpdate(
                    type="delta",
                    delta=StreamDelta(
                        index=choice.index,
                        role=delta.role,
                        content=delta.content,
                        reasoning_content=delta.reasoning_content,
                    ),
                )
            if choice.finish_reason is not None:
                finished[choice.index] = choice.finish_reason
        if frame.usage is not None:
            usage = frame.usage
            yield StreamUpdate(type="usage", usage=usage)
    raise invalid_stream()
