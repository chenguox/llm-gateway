"""构建格式提示词，并为不同上游生成可接受的原生 Schema。"""

import copy
import json

from app.schemas.chat import ChatMessage
from app.schemas.structured_output import JsonSchemaFormat

LOWERED_CONSTRAINTS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
}


def native_schema(schema: dict, *, require_all: bool) -> dict:
    """只简化上游生成约束；完整原始规则由本地 Pydantic 校验保留。"""
    result = copy.deepcopy(schema)
    constraints = {key: result.pop(key) for key in LOWERED_CONSTRAINTS if key in result}
    if constraints:
        description = result.get("description", "")
        result["description"] = (
            f"{description}\nAdditional constraints: {json.dumps(constraints, ensure_ascii=False)}"
        ).strip()
    kind = result["type"]
    if kind == "object" or isinstance(kind, list) and "object" in kind:
        result["properties"] = {
            name: native_schema(child, require_all=require_all)
            for name, child in result.get("properties", {}).items()
        }
        result["additionalProperties"] = False
        if require_all:
            result["required"] = list(result["properties"])
    if kind == "array" or isinstance(kind, list) and "array" in kind:
        result["items"] = native_schema(result["items"], require_all=require_all)
    return result


def format_messages(
    messages: list[ChatMessage], schema_format: JsonSchemaFormat | None, mode: str
) -> list[ChatMessage]:
    """在模板渲染完成后追加格式要求，不修改调用方的原始消息对象。"""
    instruction = (
        "Return exactly one valid JSON object. Do not include Markdown fences, "
        "comments, or explanatory text outside the JSON object."
    )
    if mode == "json_object":
        instruction += ' JSON syntax example only: {"example": "value"}.'
    if schema_format is not None:
        instruction += (
            " The actual keys and values must satisfy this JSON Schema; "
            "it takes precedence over the syntax example:\n"
            + json.dumps(schema_format.json_schema.schema_, ensure_ascii=False, allow_nan=False)
        )
    result = list(messages)
    for index, message in enumerate(result):
        if message.role == "system":
            result[index] = message.model_copy(
                update={"content": f"{message.content}\n\n{instruction}"}
            )
            break
    else:
        result.insert(0, ChatMessage(role="system", content=instruction))
    return result
