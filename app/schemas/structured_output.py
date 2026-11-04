"""统一结构化输出格式；这些参数由网关解释，再转换为上游协议。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class JsonObjectFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["json_object"]


class JsonSchemaSpecification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    schema_: dict[str, JsonValue] = Field(alias="schema")
    strict: Literal[True] = True


class JsonSchemaFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["json_schema"]
    json_schema: JsonSchemaSpecification


ResponseFormat = Annotated[JsonObjectFormat | JsonSchemaFormat, Field(discriminator="type")]
