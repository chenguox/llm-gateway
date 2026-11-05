"""Strict JSON validation, protocol selection and gateway-only repair retries."""

import asyncio
import copy
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.anthropic import AnthropicMessagesAdapter
from app.adapters.compatible import ChatCompletionsAdapter
from app.core.config import Settings
from app.core.errors import GatewayError
from app.main import create_app
from app.structured_output.schema import SchemaCompiler, object_validator, strict_json_object


@pytest.fixture
def person_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 20, "pattern": "^[A-Z]"},
            "age": {"type": "integer", "minimum": 0, "maximum": 150},
        },
        "required": ["name", "age"],
        "additionalProperties": False,
    }


def schema_format(schema: dict) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {"name": "person", "schema": schema, "strict": True},
    }


def payload(format_value: dict | None = None, **overrides: object) -> dict:
    return {
        "model": "general-chat",
        "messages": [{"role": "user", "content": "Extract Alice, age 28"}],
        "response_format": format_value or {"type": "json_object"},
        "max_retries": 0,
        **overrides,
    }


def reply(completion: dict, content: str | None, **overrides: object) -> httpx.Response:
    body = copy.deepcopy(completion)
    body["choices"][0]["message"]["content"] = content
    body.update(overrides)
    return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    ("field_schema", "value"),
    [
        ({"type": "string", "enum": ["yes", "no"]}, "yes"),
        ({"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 4}, 2),
        ({"type": "number", "minimum": 0.5, "multipleOf": 0.5}, 1.5),
        ({"type": "boolean"}, True),
        ({"type": "null"}, None),
        ({"type": ["string", "null"], "minLength": 2, "enum": [None, "yes"]}, None),
        ({"type": ["string", "null"], "minLength": 2}, "yes"),
        ({"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2}, [1, 2]),
        (
            {
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"],
            },
            {"enabled": False},
        ),
    ],
)
def test_supported_schema_types_and_constraints(field_schema: dict, value: object) -> None:
    schema = {"type": "object", "properties": {"value": field_schema}, "required": ["value"]}
    result = strict_json_object(json.dumps({"value": value}), SchemaCompiler().compile(schema))
    assert result == {"value": value}


def test_optional_fields_special_names_and_extra_values() -> None:
    schema = {
        "type": "object",
        "properties": {"model_dump": {"type": "string"}, "_private": {"type": "integer"}},
        "required": ["model_dump"],
    }
    validator = SchemaCompiler().compile(schema)
    assert strict_json_object('{"model_dump":"value","other":{"key":[null, true]}}', validator) == {
        "model_dump": "value",
        "other": {"key": [None, True]},
    }
    with pytest.raises(ValidationError):
        strict_json_object('{"model_dump":"value","_private":null}', validator)


@pytest.mark.parametrize(
    "content",
    [
        '{"name":"Alice","age":"28"}',
        '{"name":"Alice","age":true}',
        '{"name":"Alice","age":28.0}',
        '{"name":"Alice","age":-1}',
        '{"name":"Alice","age":151}',
        '{"name":"alice","age":28}',
        '{"name":"","age":28}',
        '{"name":"Alice"}',
        '{"name":"Alice","age":28,"extra":1}',
        '{"name":"Alice","age":null}',
    ],
)
def test_schema_validation_rejects_type_coercion_and_constraint_failures(
    person_schema: dict, content: str
) -> None:
    with pytest.raises(ValidationError):
        strict_json_object(content, SchemaCompiler().compile(person_schema))


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "null",
        "true",
        '"text"',
        "1",
        "",
        "{}{}",
        "```json\n{}\n```",
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e9999}',
    ],
)
def test_only_complete_finite_json_objects_are_allowed(content: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        strict_json_object(content, object_validator())


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "$ref": "#/definitions/person"},
        {"type": "object", "properties": {"x": {"type": "string", "format": "email"}}},
        {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}]}}},
        {"type": "object", "properties": {"x": {"type": "string", "minLength": -1}}},
        {"type": "object", "properties": {"x": {"type": "string", "minLength": 5, "maxLength": 1}}},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "["}}},
        {"type": "object", "properties": {"x": {"type": "number", "multipleOf": 0}}},
        {"type": "object", "properties": {"x": {"type": "number", "minimum": 5, "maximum": 1}}},
        {
            "type": "object",
            "properties": {"x": {"type": "number", "minimum": 1, "exclusiveMaximum": 1}},
        },
        {"type": "object", "properties": {"x": {"type": "array"}}},
        {"type": "object", "properties": {"x": {"type": ["string", "integer"]}}},
        {"type": "object", "required": ["unknown"]},
        {"type": "object", "required": [1]},
        {"type": "object", "properties": []},
        {"type": "object", "additionalProperties": {"type": "string"}},
        {"type": "object", "properties": {"x": {"type": "string", "enum": []}}},
        {"type": "object", "properties": {"x": {"type": "number", "enum": [float("nan")]}}},
    ],
)
def test_invalid_or_unimplemented_schema_is_explicitly_rejected(schema: dict) -> None:
    with pytest.raises(GatewayError) as error:
        SchemaCompiler().compile(schema)
    assert error.value.code == "STRUCTURED_OUTPUT_SCHEMA_INVALID"


def test_schema_complexity_limits() -> None:
    too_many = {
        "type": "object",
        "properties": {str(index): {"type": "string"} for index in range(257)},
    }
    too_deep = {"type": "string"}
    for _ in range(18):
        too_deep = {"type": "object", "properties": {"nested": too_deep}}
    for schema in (too_many, too_deep):
        with pytest.raises(GatewayError, match="Unsupported or invalid"):
            SchemaCompiler().compile(schema)


def test_json_object_prompt_and_validated_response(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    upstream.return_value = reply(completion, '{"name":"Alice","age":28}')
    response = chat_client.post(
        "/v1/chat/completions",
        json=payload(system_prompt={"name": "assistant", "version": "1.0.0"}),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "general-chat"
    assert body["choices"][0]["message"]["content"] == '{"name":"Alice","age":28}'
    assert body["choices"][0]["message"]["parsed"] == {"name": "Alice", "age": 28}
    assert body["structured_output"] == {
        "mode": "json_object",
        "attempts": 1,
    }
    sent = json.loads(upstream.call_args.args[0].content)
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["messages"][0]["content"].startswith("你是一名通用助手。")
    assert "JSON" in sent["messages"][0]["content"]
    assert "max_retries" not in sent
    assert "system_prompt" not in sent


def test_native_json_schema_request(
    chat_client: TestClient, upstream: Mock, completion: dict, person_schema: dict
) -> None:
    upstream.return_value = reply(completion, '{"name":"Alice","age":28}')
    response = chat_client.post("/v1/chat/completions", json=payload(schema_format(person_schema)))
    assert response.status_code == 200
    assert response.json()["structured_output"]["mode"] == "json_schema"
    sent = json.loads(upstream.call_args.args[0].content)
    native = sent["response_format"]["json_schema"]
    assert native["name"] == "person"
    assert native["strict"] is True
    assert native["schema"]["additionalProperties"] is False
    assert native["schema"]["required"] == ["name", "age"]
    assert "minimum" not in native["schema"]["properties"]["age"]
    assert "minimum" in native["schema"]["properties"]["age"]["description"]
    assert '"minimum": 0' in sent["messages"][0]["content"]


@pytest.mark.parametrize("native_mode", [False, True])
def test_schema_fallback_and_local_constraints_are_preserved(
    chat_client: TestClient,
    chat_application,
    upstream: Mock,
    completion: dict,
    person_schema: dict,
    native_mode: bool,
) -> None:
    configuration = chat_application.state.model_registry.get_configuration("general-chat")
    if not native_mode:
        configuration.capabilities.structured_output.modes[:] = ["json_object"]
    upstream.return_value = reply(completion, '{"name":"Alice","age":-1}')
    response = chat_client.post("/v1/chat/completions", json=payload(schema_format(person_schema)))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "STRUCTURED_OUTPUT_VALIDATION_FAILED"
    assert response.json()["error"]["details"]["errors"][0]["errors"][0]["location"] == ["age"]
    sent = json.loads(upstream.call_args.args[0].content)
    assert sent["response_format"]["type"] == ("json_schema" if native_mode else "json_object")
    assert '"age"' in sent["messages"][0]["content"]


def test_schema_fallback_succeeds_and_reports_actual_mode(
    chat_client: TestClient, chat_application, upstream: Mock, completion: dict, person_schema: dict
) -> None:
    chat_application.state.model_registry.get_configuration(
        "general-chat"
    ).capabilities.structured_output.modes[:] = ["json_object"]
    upstream.return_value = reply(completion, '{"name":"Alice","age":28}')
    response = chat_client.post("/v1/chat/completions", json=payload(schema_format(person_schema)))
    assert response.status_code == 200
    assert response.json()["structured_output"]["mode"] == "json_object"


def test_schema_repair_preserves_task_and_sampling_parameters(
    chat_client: TestClient, upstream: Mock, completion: dict, person_schema: dict
) -> None:
    upstream.side_effect = [
        reply(completion, '{"name":"Alice","age":"28"}'),
        reply(completion, '{"name":"Alice","age":28}'),
    ]
    response = chat_client.post(
        "/v1/chat/completions",
        json=payload(
            schema_format(person_schema), max_retries=2, temperature=0.4, top_p=0.8, max_tokens=100
        ),
    )
    assert response.status_code == 200
    assert upstream.call_count == 2
    assert response.json()["choices"][0]["message"]["parsed"]["age"] == 28
    assert response.json()["structured_output"] == {
        "mode": "json_schema",
        "attempts": 2,
    }
    assert response.json()["usage"] == {"input_tokens": 24, "output_tokens": 16, "total_tokens": 40}
    first, second = [json.loads(call.args[0].content) for call in upstream.call_args_list]
    original_messages = first.pop("messages")
    repaired_messages = second.pop("messages")
    assert first == second
    assert repaired_messages[:-2] == original_messages
    assert repaired_messages[-2] == {"role": "assistant", "content": '{"name":"Alice","age":"28"}'}
    assert '"age"' in repaired_messages[-1]["content"]
    assert "integer" in repaired_messages[-1]["content"]
    assert "smallest necessary changes" in repaired_messages[-1]["content"]


@pytest.mark.parametrize("max_retries", [0, 1, 3])
def test_retry_budget_and_bounded_repair_context(
    chat_client: TestClient, upstream: Mock, completion: dict, max_retries: int
) -> None:
    upstream.side_effect = [
        reply(completion, f"private-invalid-output-{index}") for index in range(max_retries + 1)
    ]
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=max_retries))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "STRUCTURED_OUTPUT_VALIDATION_FAILED"
    assert response.json()["error"]["details"]["attempts"] == max_retries + 1
    assert "private-invalid-output" not in response.text
    assert upstream.call_count == max_retries + 1
    requests = [json.loads(call.args[0].content) for call in upstream.call_args_list]
    for index, request in enumerate(requests[1:], start=1):
        assert request["messages"][:-2] == requests[0]["messages"]
        assert request["messages"][-2]["content"] == f"private-invalid-output-{index - 1}"


def test_default_retry_budget_is_one(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    upstream.return_value = reply(completion, "invalid")
    request = payload()
    request.pop("max_retries")
    response = chat_client.post("/v1/chat/completions", json=request)
    assert response.status_code == 502
    assert upstream.call_count == 2


@pytest.mark.parametrize("missing_content", [None, "", "[]", "```json\n{}\n```"])
def test_invalid_json_content_is_never_returned_as_success(
    chat_client: TestClient, upstream: Mock, completion: dict, missing_content: str | None
) -> None:
    upstream.return_value = reply(completion, missing_content)
    response = chat_client.post("/v1/chat/completions", json=payload())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "STRUCTURED_OUTPUT_VALIDATION_FAILED"


def test_every_choice_is_validated(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    body = copy.deepcopy(completion)
    body["choices"][0]["message"]["content"] = "{}"
    body["choices"].append({"index": 1, "message": {"role": "assistant", "content": "invalid"}})
    upstream.return_value = httpx.Response(200, json=body)
    response = chat_client.post("/v1/chat/completions", json=payload())
    assert response.status_code == 502
    assert response.json()["error"]["details"]["errors"][0]["choice_index"] == 1


def test_truncated_json_is_repaired_even_if_parseable(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    body = copy.deepcopy(completion)
    body["choices"][0].update(finish_reason="length")
    body["choices"][0]["message"]["content"] = "{}"
    upstream.return_value = httpx.Response(200, json=body)
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=1))
    assert response.status_code == 502
    assert upstream.call_count == 2
    assert "truncated" in response.text


@pytest.mark.parametrize("finish_reason", ["content_filter", "refusal"])
def test_refusal_is_not_retried(
    chat_client: TestClient, upstream: Mock, completion: dict, finish_reason: str
) -> None:
    body = copy.deepcopy(completion)
    body["choices"][0]["finish_reason"] = finish_reason
    upstream.return_value = httpx.Response(200, json=body)
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=3))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "STRUCTURED_OUTPUT_REFUSED"
    upstream.assert_called_once()


@pytest.mark.parametrize("kind", ["http", "timeout", "connection", "envelope", "json"])
def test_non_content_errors_are_not_retried(
    chat_client: TestClient, upstream: Mock, kind: str
) -> None:
    if kind == "http":
        upstream.return_value = httpx.Response(500)
    elif kind == "timeout":
        upstream.side_effect = httpx.ReadTimeout("timeout")
    elif kind == "connection":
        upstream.side_effect = httpx.ConnectError("unavailable")
    elif kind == "envelope":
        upstream.return_value = httpx.Response(200, json={})
    else:
        upstream.return_value = httpx.Response(200, content=b"not json")
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=3))
    assert response.status_code in {502, 504}
    upstream.assert_called_once()


@pytest.mark.parametrize("value", [-1, 4, True, "1", 1.5, None])
def test_invalid_retry_parameter_is_rejected(
    chat_client: TestClient, upstream: Mock, value: object
) -> None:
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=value))
    assert response.status_code == 422
    upstream.assert_not_called()


def test_invalid_schema_is_rejected_before_call(chat_client: TestClient, upstream: Mock) -> None:
    response = chat_client.post(
        "/v1/chat/completions", json=payload(schema_format({"type": "object", "$ref": "#"}))
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "STRUCTURED_OUTPUT_SCHEMA_INVALID"
    upstream.assert_not_called()


def test_missing_usage_is_not_reported_as_complete_total(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    upstream.side_effect = [reply(completion, "invalid", usage=None), reply(completion, "{}")]
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=1))
    assert response.status_code == 200
    assert response.json()["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


@pytest.fixture
def anthropic_client(
    model_config_path: Path,
    model_configuration: dict,
    upstream: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    model = model_configuration["models"][0]
    model.update(provider="anthropic", base_url="https://provider.test/anthropic")
    model["capabilities"]["structured_output"]["modes"] = ["json_schema"]
    model_config_path.write_text(json.dumps(model_configuration), encoding="utf-8")
    monkeypatch.setenv("TEST_GENERAL_CHAT_API_KEY", "claude-test-key")
    upstream.return_value = httpx.Response(
        200,
        json={
            "id": "msg-test",
            "type": "message",
            "role": "assistant",
            "model": "provider-model",
            "content": [{"type": "text", "text": '{"name":"Alice","age":28}'}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    )
    with TestClient(
        create_app(Settings(_env_file=None, models_config_path=model_config_path))
    ) as client:
        yield client


def test_anthropic_native_structured_request(
    anthropic_client: TestClient, upstream: Mock, person_schema: dict
) -> None:
    response = anthropic_client.post(
        "/v1/chat/completions", json=payload(schema_format(person_schema))
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["parsed"] == {"name": "Alice", "age": 28}
    sent_request = upstream.call_args.args[0]
    sent = json.loads(sent_request.content)
    assert str(sent_request.url) == "https://provider.test/anthropic/v1/messages"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"]["required"] == ["name", "age"]
    assert "minimum" not in sent["output_config"]["format"]["schema"]["properties"]["age"]
    assert "JSON" in sent["system"]
    assert "response_format" not in sent
    assert "max_retries" not in sent
    assert response.json()["model"] == "general-chat"


def test_json_object_is_not_faked_for_messages(
    anthropic_client: TestClient, upstream: Mock
) -> None:
    response = anthropic_client.post("/v1/chat/completions", json=payload())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CAPABILITY_NOT_SUPPORTED"
    upstream.assert_not_called()


def test_messages_repair_uses_the_same_system_and_native_schema(
    anthropic_client: TestClient, upstream: Mock, person_schema: dict
) -> None:
    valid = upstream.return_value.json()
    invalid = copy.deepcopy(valid)
    invalid["content"][0]["text"] = '{"name":"Alice","age":"28"}'
    upstream.side_effect = [httpx.Response(200, json=invalid), httpx.Response(200, json=valid)]
    response = anthropic_client.post(
        "/v1/chat/completions", json=payload(schema_format(person_schema), max_retries=1)
    )
    assert response.status_code == 200
    assert response.json()["structured_output"] == {
        "mode": "json_schema",
        "attempts": 2,
    }
    first, second = [json.loads(call.args[0].content) for call in upstream.call_args_list]
    assert second["system"] == first["system"]
    assert second["output_config"] == first["output_config"]
    assert second["messages"][:-2] == first["messages"]
    assert second["messages"][-2]["content"] == invalid["content"][0]["text"]
    assert all(message["role"] != "system" for message in second["messages"])


def test_content_error_details_do_not_echo_invalid_values(
    chat_client: TestClient, upstream: Mock, completion: dict, person_schema: dict
) -> None:
    upstream.return_value = reply(completion, '{"name":"Alice","age":"private-value"}')
    response = chat_client.post("/v1/chat/completions", json=payload(schema_format(person_schema)))
    assert response.status_code == 502
    assert "private-value" not in response.text
    error = response.json()["error"]["details"]["errors"][0]["errors"][0]
    assert error["location"] == ["age"]
    assert "input" not in error
    assert "ctx" not in error


def test_plain_text_request_ignores_structured_retry_budget(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    request = payload(max_retries=3)
    request.pop("response_format")
    response = chat_client.post("/v1/chat/completions", json=request)
    assert response.status_code == 200
    assert response.json() == {
        **completion,
        "model": "general-chat",
        "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    }
    upstream.assert_called_once()
    assert "max_retries" not in json.loads(upstream.call_args.args[0].content)


@pytest.mark.parametrize(
    "format_value",
    [
        {"type": "xml"},
        {"type": "json_schema"},
        {"type": "json_object", "extra": 1},
        {"type": "json_schema", "json_schema": {"name": "", "schema": {"type": "object"}}},
        {
            "type": "json_schema",
            "json_schema": {"name": "ok", "schema": {"type": "object"}, "strict": False},
        },
    ],
)
def test_malformed_format_is_rejected_without_call(
    chat_client: TestClient, upstream: Mock, format_value: dict
) -> None:
    response = chat_client.post("/v1/chat/completions", json=payload(format_value))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    upstream.assert_not_called()


def test_transport_failure_during_repair_stops_further_attempts(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    upstream.side_effect = [reply(completion, "invalid"), httpx.ConnectError("unavailable")]
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=3))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_CONNECTION_ERROR"
    assert upstream.call_count == 2


def test_refusal_field_is_not_retried(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    body = copy.deepcopy(completion)
    body["choices"][0]["message"].update(content=None, refusal="Declined")
    upstream.return_value = httpx.Response(200, json=body)
    response = chat_client.post("/v1/chat/completions", json=payload(max_retries=3))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "STRUCTURED_OUTPUT_REFUSED"
    upstream.assert_called_once()


@pytest.mark.parametrize("adapter_class", [ChatCompletionsAdapter, AnthropicMessagesAdapter])
def test_owned_http_transport_disables_retries(
    monkeypatch: pytest.MonkeyPatch, adapter_class: type
) -> None:
    transport_class = httpx.AsyncHTTPTransport
    arguments = []

    def transport(**kwargs):
        arguments.append(kwargs)
        return transport_class(**kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)

    async def run():
        async with adapter_class(api_key="test-key", base_url="https://provider.test"):
            pass

    asyncio.run(run())
    assert arguments == [{"retries": 0}]
