"""Public chat completions contract and mocked provider integration."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_model_registry
from app.core.config import Settings
from app.main import create_app
from app.models.registry import ModelRegistry
from app.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    LLMRequest,
    LLMResponse,
)


def test_chat_forwards_request_and_returns_unified_response(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    payload = {
        "model": "general-chat",
        "messages": [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ],
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 128,
        "stream": False,
    }

    response = chat_client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.json() == {**completion, "model": "general-chat"}
    upstream.assert_called_once()
    request = upstream.call_args.args[0]
    assert request.method == "POST"
    assert str(request.url) == "https://provider.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer unit-test-key"
    assert request.extensions["timeout"]["read"] == 5
    assert json.loads(request.content) == {**payload, "model": "deepseek-chat"}


def test_chat_defaults_to_non_streaming_and_omits_unset_options(
    chat_client: TestClient, upstream: Mock
) -> None:
    payload = {"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]}

    response = chat_client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert json.loads(upstream.call_args.args[0].content) == {
        **payload,
        "model": "deepseek-chat",
        "stream": False,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": ""},
        {"model": "   "},
        {"model": None},
        {"messages": []},
        {"messages": [{"role": "tool", "content": "Hello"}]},
        {"messages": [{"role": "user"}]},
        {"messages": [{"role": "user", "content": None}]},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"top_p": -0.1},
        {"top_p": 1.1},
        {"max_tokens": 0},
        {"max_tokens": 1.5},
        {"stream": "true"},
        {"stream": 1},
        {"tools": []},
    ],
)
def test_invalid_chat_request_is_rejected_without_calling_provider(
    chat_client: TestClient, upstream: Mock, overrides: dict
) -> None:
    payload = {"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]}

    response = chat_client.post("/v1/chat/completions", json={**payload, **overrides})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]
    upstream.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"model": "general-chat"}, None])
def test_missing_chat_fields_are_rejected(
    chat_client: TestClient, upstream: Mock, body: dict | None
) -> None:
    response = chat_client.post("/v1/chat/completions", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    upstream.assert_not_called()


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_missing_model_configuration_keeps_health_available(
    api_key: str | None,
    model_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if api_key is not None:
        monkeypatch.setenv("TEST_GENERAL_CHAT_API_KEY", api_key)
    application = create_app(Settings(_env_file=None, models_config_path=model_config_path))
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"
        assert "TEST_GENERAL_CHAT_API_KEY" not in response.text
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_upstream_http_errors_are_mapped_and_sanitized(
    chat_client: TestClient, upstream: Mock, status: int
) -> None:
    upstream.return_value = httpx.Response(
        status, json={"error": {"message": "private upstream detail: unit-test-key"}}
    )

    response = chat_client.post(
        "/v1/chat/completions",
        json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == (429 if status == 429 else 502)
    assert response.json()["error"]["code"] == (
        "UPSTREAM_RATE_LIMITED" if status == 429 else "UPSTREAM_HTTP_ERROR"
    )
    assert "private upstream" not in response.text
    assert "unit-test-key" not in response.text


@pytest.mark.parametrize(
    ("error_type", "status", "code"),
    [
        (httpx.ConnectTimeout, 504, "UPSTREAM_TIMEOUT"),
        (httpx.ReadTimeout, 504, "UPSTREAM_TIMEOUT"),
        (httpx.ConnectError, 502, "UPSTREAM_CONNECTION_ERROR"),
        (httpx.RemoteProtocolError, 502, "UPSTREAM_CONNECTION_ERROR"),
    ],
)
def test_upstream_network_errors_are_mapped_and_sanitized(
    chat_client: TestClient, upstream: Mock, error_type: type[Exception], status: int, code: str
) -> None:
    upstream.side_effect = error_type("private upstream detail: unit-test-key")

    response = chat_client.post(
        "/v1/chat/completions",
        json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "private upstream" not in response.text
    assert "unit-test-key" not in response.text


@pytest.mark.parametrize(
    "provider_response",
    [
        httpx.Response(200, text="not JSON: unit-test-key"),
        httpx.Response(200, json={"private": "unit-test-key"}),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=None),
    ],
)
def test_invalid_upstream_response_returns_bad_gateway(
    chat_client: TestClient, upstream: Mock, provider_response: httpx.Response
) -> None:
    upstream.return_value = provider_response

    response = chat_client.post(
        "/v1/chat/completions",
        json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_INVALID_RESPONSE"
    assert "unit-test-key" not in response.text


def test_response_preserves_optional_fields_and_provider_extensions(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    completion.pop("usage")
    completion["choices"][0]["message"]["content"] = None
    completion["choices"][0]["message"]["provider_detail"] = "extended"
    completion["system_fingerprint"] = "provider-fingerprint"
    upstream.return_value = httpx.Response(200, json=completion)

    response = chat_client.post(
        "/v1/chat/completions",
        json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    assert response.json() == {**completion, "model": "general-chat"}


def test_http_client_is_reused_and_closed_with_application(
    chat_application: FastAPI, upstream: Mock, upstream_clients: list[httpx.AsyncClient]
) -> None:
    with TestClient(chat_application) as client:
        assert len(upstream_clients) == 2
        assert all(not upstream_client.is_closed for upstream_client in upstream_clients)
        adapter = chat_application.state.model_registry.get_adapter("general-chat")
        for _ in range(2):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "general-chat", "messages": [{"role": "user", "content": "Hi"}]},
            )
            assert response.status_code == 200
            assert chat_application.state.model_registry.get_adapter("general-chat") is adapter
        assert len(upstream_clients) == 2
        assert upstream.call_count == 2

    assert all(upstream_client.is_closed for upstream_client in upstream_clients)


def test_model_registry_dependency_can_be_overridden(
    application: FastAPI, client: TestClient, completion: dict
) -> None:
    registry = AsyncMock(spec=ModelRegistry)
    registry.complete.return_value = LLMResponse.model_validate(completion)
    application.dependency_overrides[get_model_registry] = lambda: registry

    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    registry.complete.assert_awaited_once()
    assert isinstance(registry.complete.call_args.args[0], LLMRequest)
    assert registry.complete.call_args.args[0].model == "test-model"


def test_openapi_exposes_unified_chat_protocol(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/chat/completions"]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LLMRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LLMResponse"
    }


def test_previous_protocol_names_remain_compatible() -> None:
    assert ChatCompletionRequest is LLMRequest
    assert ChatCompletionResponse is LLMResponse


def test_selected_prompt_replaces_system_messages_before_calling_provider(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    response = chat_client.post(
        "/v1/chat/completions",
        json={
            "model": "general-chat",
            "system_prompt": {
                "name": "customer_support",
                "version": "1.0.0",
                "parameters": {"product": "示例产品", "language": "中文"},
            },
            "messages": [
                {"role": "system", "content": "old system"},
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
                {"role": "system", "content": "another old system"},
                {"role": "user", "content": "How can I use it?"},
            ],
            "max_tokens": 128,
        },
    )

    assert response.status_code == 200
    assert response.json() == {**completion, "model": "general-chat"}
    upstream.assert_called_once()
    payload = json.loads(upstream.call_args.args[0].content)
    assert payload == {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是示例产品的客服助手。请使用中文回答用户的问题，不要编造产品功能或服务承诺。"
                ),
            },
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "How can I use it?"},
        ],
        "max_tokens": 128,
        "stream": False,
    }


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.0.0", "你是一名通用助手。请使用中文回答用户的问题。"),
        (
            "2.0.0",
            "你是一名通用助手。请使用中文回答，表达风格为简洁。回答应准确、清晰；不确定时说明不确定性。",
        ),
    ],
)
def test_selected_prompt_uses_exact_version_and_default_parameters(
    chat_client: TestClient, upstream: Mock, version: str, expected: str
) -> None:
    response = chat_client.post(
        "/v1/chat/completions",
        json={
            "model": "general-chat",
            "system_prompt": {"name": "assistant", "version": version},
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 200
    payload = json.loads(upstream.call_args.args[0].content)
    assert payload["messages"][0] == {"role": "system", "content": expected}
    assert payload["messages"][1] == {"role": "user", "content": "Hello"}
    assert "system_prompt" not in payload


@pytest.mark.parametrize(
    ("selection", "status", "code"),
    [
        ({"name": "missing", "version": "1.0.0"}, 404, "PROMPT_TEMPLATE_NOT_FOUND"),
        ({"name": "assistant", "version": "latest"}, 404, "PROMPT_TEMPLATE_NOT_FOUND"),
        ({"name": "customer_support", "version": "1.0.0"}, 422, "PROMPT_PARAMETER_ERROR"),
        (
            {"name": "assistant", "version": "1.0.0", "parameters": {"unknown": "private-value"}},
            422,
            "PROMPT_PARAMETER_ERROR",
        ),
        ({"name": "assistant"}, 422, "VALIDATION_ERROR"),
        ({"version": "1.0.0"}, 422, "VALIDATION_ERROR"),
        ({"name": "", "version": "1.0.0"}, 422, "VALIDATION_ERROR"),
        ({"name": "assistant", "version": " "}, 422, "VALIDATION_ERROR"),
        ({"name": "assistant", "version": "1.0.0", "parameters": []}, 422, "VALIDATION_ERROR"),
    ],
)
def test_prompt_errors_prevent_upstream_calls(
    chat_client: TestClient, upstream: Mock, selection: dict, status: int, code: str
) -> None:
    response = chat_client.post(
        "/v1/chat/completions",
        json={
            "model": "general-chat",
            "system_prompt": selection,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "private-value" not in response.text
    upstream.assert_not_called()
