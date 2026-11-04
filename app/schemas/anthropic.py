"""校验 Messages 协议的上游响应，供适配器转换为统一响应。"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class AnthropicTextBlock(BaseModel):
    type: Literal["text"]
    text: str


class AnthropicThinkingBlock(BaseModel):
    type: Literal["thinking"]
    thinking: str


class AnthropicRedactedThinkingBlock(BaseModel):
    type: Literal["redacted_thinking"]
    data: str


ContentBlock = Annotated[
    AnthropicTextBlock | AnthropicThinkingBlock | AnthropicRedactedThinkingBlock,
    Field(discriminator="type"),
]


class AnthropicUsage(BaseModel):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    cache_creation_input_tokens: int = Field(default=0, ge=0, strict=True)
    cache_read_input_tokens: int = Field(default=0, ge=0, strict=True)


class AnthropicMessageResponse(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["message"]
    role: Literal["assistant"]
    model: str = Field(min_length=1)
    content: list[ContentBlock]
    stop_reason: str | None = None
    usage: AnthropicUsage
