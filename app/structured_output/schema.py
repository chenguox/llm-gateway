"""将明确支持的 JSON Schema 子集编译为严格 Pydantic 模型。"""

import json
import math
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    create_model,
)
from pydantic_core import SchemaError

from app.core.errors import GatewayError

COMMON = {"type", "title", "description", "enum"}
KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength", "pattern"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"},
    "boolean": set(),
    "null": set(),
}
CONSTRAINTS = {
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "multipleOf": "multiple_of",
    "minLength": "min_length",
    "maxLength": "max_length",
    "minItems": "min_length",
    "maxItems": "max_length",
    "pattern": "pattern",
}


def invalid_schema(path: str, message: str) -> GatewayError:
    return GatewayError(
        422,
        "STRUCTURED_OUTPUT_SCHEMA_INVALID",
        "Unsupported or invalid structured output schema",
        details={"location": path, "reason": message},
    )


class SchemaCompiler:
    """拒绝未实现的关键字，防止接收 Schema 后悄悄忽略其约束。"""

    def __init__(self) -> None:
        self._nodes = 0

    def compile(self, schema: dict) -> TypeAdapter:
        if schema.get("type") != "object":
            raise invalid_schema("$", "The root schema must have type object")
        try:
            json.dumps(schema, allow_nan=False)
            return TypeAdapter(self._compile(schema, "$", 0))
        except GatewayError:
            raise
        except (TypeError, ValueError, OverflowError, SchemaError) as exc:
            raise invalid_schema("$", "Schema constraints cannot be compiled") from exc

    def _compile(self, schema: object, path: str, depth: int) -> Any:
        self._nodes += 1
        if depth > 16 or self._nodes > 256:
            raise invalid_schema(path, "Schema exceeds the depth or node limit")
        if not isinstance(schema, dict):
            raise invalid_schema(path, "Each schema must be an object")
        kind = schema.get("type")
        if isinstance(kind, list):
            if (
                len(kind) != 2
                or any(not isinstance(item, str) for item in kind)
                or kind.count("null") != 1
            ):
                raise invalid_schema(path, "Only nullable two-type unions are supported")
            non_null = next(item for item in kind if item != "null")
            branch = {**schema, "type": non_null}
            branch.pop("enum", None)
            annotation = self._compile(branch, path, depth + 1) | None
            return self._enum(annotation, schema, path)
        if not isinstance(kind, str) or kind not in KEYWORDS:
            raise invalid_schema(path, "An explicit supported type is required")
        unknown = set(schema) - COMMON - KEYWORDS[kind]
        if unknown:
            raise invalid_schema(path, f"Unsupported keywords: {', '.join(sorted(unknown))}")
        for name in ("title", "description"):
            if name in schema and not isinstance(schema[name], str):
                raise invalid_schema(path, f"{name} must be a string")
        self._check_constraints(schema, path)
        if kind == "object":
            annotation = self._object(schema, path, depth)
        elif kind == "array":
            if "items" not in schema:
                raise invalid_schema(path, "Array items must have an explicit schema")
            annotation = list[self._compile(schema["items"], f"{path}.items", depth + 1)]
        else:
            annotation = {
                "string": str,
                "integer": int,
                "number": float,
                "boolean": bool,
                "null": type(None),
            }[kind]
        constraints = {
            target: schema[source] for source, target in CONSTRAINTS.items() if source in schema
        }
        if constraints:
            annotation = Annotated[annotation, Field(**constraints)]
        return self._enum(annotation, schema, path)

    def _object(self, schema: dict, path: str, depth: int) -> type[BaseModel]:
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        extra = schema.get("additionalProperties", True)
        if not isinstance(properties, dict):
            raise invalid_schema(path, "properties must be an object")
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) for name in required)
            or len(set(required)) != len(required)
            or set(required) - set(properties)
        ):
            raise invalid_schema(path, "required must contain unique declared property names")
        if not isinstance(extra, bool):
            raise invalid_schema(path, "additionalProperties must be a boolean")
        fields = {}
        for index, (name, child) in enumerate(properties.items()):
            annotation = self._compile(child, f"{path}.properties.{name}", depth + 1)
            fields[f"json_field_{index}"] = (
                annotation,
                Field(default=... if name in required else None, alias=name),
            )
        return create_model(
            f"StructuredObject{self._nodes}",
            __config__=ConfigDict(
                strict=True, extra="allow" if extra else "forbid", allow_inf_nan=False
            ),
            **fields,
        )

    def _enum(self, annotation: Any, schema: dict, path: str) -> Any:
        if "enum" not in schema:
            return annotation
        values = schema["enum"]
        if (
            not isinstance(values, list)
            or not values
            or any(isinstance(value, (dict, list)) for value in values)
        ):
            raise invalid_schema(path, "enum must contain primitive JSON values")

        def validate(value: Any) -> Any:
            comparable = value.model_dump(by_alias=True) if isinstance(value, BaseModel) else value
            for allowed in values:
                if isinstance(comparable, bool) != isinstance(allowed, bool):
                    continue
                if comparable == allowed:
                    return value
            raise ValueError("Value is not one of the allowed enum values")

        return Annotated[annotation, AfterValidator(validate)]

    def _check_constraints(self, schema: dict, path: str) -> None:
        for name in CONSTRAINTS:
            if name not in schema:
                continue
            value = schema[name]
            if name == "pattern":
                valid = isinstance(value, str)
            elif name in {"minLength", "maxLength", "minItems", "maxItems"}:
                valid = type(value) is int and value >= 0
            else:
                valid = type(value) in (int, float) and math.isfinite(value)
                if name == "multipleOf":
                    valid = valid and value > 0
            if not valid:
                raise invalid_schema(path, f"Invalid {name} constraint")
        for low, high in (("minLength", "maxLength"), ("minItems", "maxItems")):
            if low in schema and high in schema and schema[low] > schema[high]:
                raise invalid_schema(path, f"{low} cannot exceed {high}")
        lower = [
            (schema[key], key == "exclusiveMinimum")
            for key in ("minimum", "exclusiveMinimum")
            if key in schema
        ]
        upper = [
            (schema[key], key == "exclusiveMaximum")
            for key in ("maximum", "exclusiveMaximum")
            if key in schema
        ]
        if lower and upper:
            minimum, exclusive_minimum = max(lower)
            maximum, exclusive_maximum = min(upper, key=lambda item: (item[0], not item[1]))
            if minimum > maximum or (
                minimum == maximum and (exclusive_minimum or exclusive_maximum)
            ):
                raise invalid_schema(path, "Numeric bounds have no valid values")


def strict_json_object(content: str, validator: TypeAdapter) -> dict[str, JsonValue]:
    """先拒绝非 JSON 数值，再通过 Pydantic 严格校验，保留缺省字段的缺省状态。"""

    def reject_constant(value: str) -> None:
        raise ValueError("Non-finite numbers are not valid JSON")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite numbers are not valid JSON")
        return number

    decoded = json.loads(content, parse_constant=reject_constant, parse_float=finite_float)
    validated = validator.validate_python(decoded, strict=True)
    if isinstance(validated, BaseModel):
        return validated.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return validated


def object_validator() -> TypeAdapter:
    return TypeAdapter(dict[str, JsonValue], config=ConfigDict(strict=True, allow_inf_nan=False))
