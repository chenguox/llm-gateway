"""Typed chat completion requests and responses."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    StringConstraints,
    computed_field,
    model_validator,
)

from app.schemas.prompts import PromptSelection
from app.schemas.structured_output import ResponseFormat


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(
        description="Logical model name from GET /v1/models; provider model names are private."
    )
    messages: list[ChatMessage] = Field(min_length=1)
    system_prompt: PromptSelection | None = Field(
        default=None,
        exclude=True,
        description="Select a versioned system prompt template to replace system messages.",
    )
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool = Field(default=False, strict=True)
    response_format: ResponseFormat | None = Field(
        default=None, exclude=True, description="Validated JSON object or JSON Schema output."
    )
    max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        strict=True,
        exclude=True,
        description="Gateway repair retries after structured output validation fails.",
    )


class AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["assistant"]
    content: str | None = None
    reasoning_content: str | None = None
    parsed: dict[str, JsonValue] | None = None


class ChatCompletionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    _details: dict[str, int | None] = PrivateAttr(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def derive_total(cls, value: object) -> object:
        if isinstance(value, dict) and "total_tokens" in value:
            value = {name: counter for name, counter in value.items() if name != "total_tokens"}
        return value

    @computed_field
    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


class ChatCompletionChoice(BaseModel):
    index: int = Field(ge=0)
    message: AssistantMessage
    finish_reason: str | None = None


class StructuredOutputInfo(BaseModel):
    mode: Literal["json_object", "json_schema"]
    attempts: int = Field(ge=1)


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[ChatCompletionChoice] = Field(min_length=1)
    usage: ChatCompletionUsage | None = None
    structured_output: StructuredOutputInfo | None = None


ChatCompletionRequest = LLMRequest
ChatCompletionResponse = LLMResponse
