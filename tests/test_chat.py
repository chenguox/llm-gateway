"""Public chat completions contract and mocked provider integration."""

import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, Mock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.base import ModelAdapter
from app.api.dependencies import get_model_adapter
from app.core.config import Settings
from app.main import create_app
from app.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    LLMRequest,
    LLMResponse,
)


@pytest.fixture
def completion() -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello!",
                    "reasoning_content": "A greeting is appropriate.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


@pytest.fixture
def upstream_clients() -> list[httpx.AsyncClient]:
    return []


@pytest.fixture
def upstream(
    monkeypatch: pytest.MonkeyPatch,
    completion: dict,
    upstream_clients: list[httpx.AsyncClient],
) -> Mock:
    handler = Mock(return_value=httpx.Response(200, json=completion))
    client_class = httpx.AsyncClient

    def create_client() -> httpx.AsyncClient:
        client = client_class(transport=httpx.MockTransport(handler))
        upstream_clients.append(client)
        return client

    monkeypatch.setattr("app.adapters.deepseek.httpx.AsyncClient", create_client)
    return handler


@pytest.fixture
def chat_application(upstream: Mock) -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            log_level="WARNING",
            deepseek_api_key="unit-test-key",
            deepseek_base_url="https://provider.test/v1",
            deepseek_timeout_seconds=5,
        )
    )


@pytest.fixture
def chat_client(chat_application: FastAPI) -> Iterator[TestClient]:
    with TestClient(chat_application, raise_server_exceptions=False) as client:
        yield client


def test_chat_forwards_request_and_returns_unified_response(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    payload = {
        "model": "deepseek-chat",
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
    assert response.json() == completion
    upstream.assert_called_once()
    request = upstream.call_args.args[0]
    assert request.method == "POST"
    assert str(request.url) == "https://provider.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer unit-test-key"
    assert request.extensions["timeout"]["read"] == 5
    assert json.loads(request.content) == payload


def test_chat_defaults_to_non_streaming_and_omits_unset_options(
    chat_client: TestClient, upstream: Mock
) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]}

    response = chat_client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert json.loads(upstream.call_args.args[0].content) == {**payload, "stream": False}


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
        {"stream": True},
        {"tools": []},
    ],
)
def test_invalid_chat_request_is_rejected_without_calling_provider(
    chat_client: TestClient, upstream: Mock, overrides: dict
) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]}

    response = chat_client.post("/v1/chat/completions", json={**payload, **overrides})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"]
    upstream.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"model": "deepseek-chat"}, None])
def test_missing_chat_fields_are_rejected(
    chat_client: TestClient, upstream: Mock, body: dict | None
) -> None:
    response = chat_client.post("/v1/chat/completions", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    upstream.assert_not_called()


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_missing_provider_configuration_keeps_health_available(api_key: str | None) -> None:
    application = create_app(Settings(_env_file=None, deepseek_api_key=api_key))
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"
        assert "DEEPSEEK_API_KEY" not in response.text
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
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]},
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
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]},
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
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]},
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
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    assert response.json() == completion


def test_http_client_is_reused_and_closed_with_application(
    chat_application: FastAPI, upstream: Mock, upstream_clients: list[httpx.AsyncClient]
) -> None:
    with TestClient(chat_application) as client:
        assert len(upstream_clients) == 1
        assert not upstream_clients[0].is_closed
        adapter = chat_application.state.model_adapter
        for _ in range(2):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hi"}]},
            )
            assert response.status_code == 200
            assert chat_application.state.model_adapter is adapter
        assert len(upstream_clients) == 1
        assert upstream.call_count == 2

    assert upstream_clients[0].is_closed
    assert chat_application.state.model_adapter is None


def test_adapter_dependency_can_be_overridden(
    application: FastAPI, client: TestClient, completion: dict
) -> None:
    adapter = AsyncMock(spec=ModelAdapter)
    adapter.complete.return_value = LLMResponse.model_validate(completion)
    application.dependency_overrides[get_model_adapter] = lambda: adapter

    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    adapter.complete.assert_awaited_once()
    assert isinstance(adapter.complete.call_args.args[0], LLMRequest)
    assert adapter.complete.call_args.args[0].model == "test-model"


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
