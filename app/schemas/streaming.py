"""Provider-independent incremental updates and public SSE envelopes."""

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.chat import ChatCompletionUsage


class StreamDelta(BaseModel):
    index: int = Field(ge=0)
    role: Literal["assistant"] | None = None
    content: str | None = None
    reasoning_content: str | None = None


class StreamFinishReason(BaseModel):
    index: int = Field(ge=0)
    finish_reason: str


class StreamError(BaseModel):
    code: str
    message: str


class StreamUpdate(BaseModel):
    type: Literal["delta", "usage", "completed"]
    delta: StreamDelta | None = None
    usage: ChatCompletionUsage | None = None
    finish_reasons: list[StreamFinishReason] | None = None


class StreamEvent(BaseModel):
    type: Literal["started", "delta", "usage", "completed", "failed", "cancelled"]
    stream_id: str
    sequence: int = Field(ge=1)
    model: str
    delta: StreamDelta | None = None
    usage: ChatCompletionUsage | None = None
    finish_reasons: list[StreamFinishReason] | None = None
    error: StreamError | None = None
    reason: str | None = None
