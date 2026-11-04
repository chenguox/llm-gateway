"""Versioned system prompt selections and catalog metadata."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

PromptIdentifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PromptSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: PromptIdentifier
    version: PromptIdentifier
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class PromptParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    required: bool = True
    default: JsonValue = None


class PromptTemplateInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: PromptIdentifier
    version: PromptIdentifier
    description: str = ""
    parameters: dict[str, PromptParameter] = Field(default_factory=dict)


class PromptTemplate(PromptTemplateInfo):
    content: str = Field(min_length=1)


class PromptTemplateList(BaseModel):
    data: list[PromptTemplateInfo]
