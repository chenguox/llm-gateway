"""Typed chat completion requests, responses and future stream chunks."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.prompts import PromptSelection


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    messages: list[ChatMessage] = Field(min_length=1)
    system_prompt: PromptSelection | None = Field(
        default=None,
        exclude=True,
        description="Select a versioned system prompt template to replace system messages.",
    )
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: Literal[False] = False


class AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"]
    content: str | None = None
    reasoning_content: str | None = None


class ChatCompletionUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ChatCompletionChoice(BaseModel):
    index: int = Field(ge=0)
    message: AssistantMessage
    finish_reason: str | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[ChatCompletionChoice] = Field(min_length=1)
    usage: ChatCompletionUsage | None = None


ChatCompletionRequest = LLMRequest
ChatCompletionResponse = LLMResponse


class ChatCompletionDelta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    reasoning_content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = Field(ge=0)
    delta: ChatCompletionDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """Reserved return type for future streaming implementations."""

    id: str
    object: Literal["chat.completion.chunk"]
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: ChatCompletionUsage | None = None
