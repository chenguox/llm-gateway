"""Messages protocol conversion and logical-model gateway integration."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.anthropic import AnthropicMessagesAdapter
from app.core.config import Settings
from app.core.errors import GatewayError
from app.main import create_app
from app.schemas.chat import LLMRequest


@pytest.fixture
def message_response() -> dict:
    return {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-v4-flash",
        "content": [
            {"type": "thinking", "thinking": "First thought.", "signature": "private"},
            {"type": "redacted_thinking", "data": "private encrypted reasoning"},
            {"type": "text", "text": "Hello"},
            {"type": "thinking", "thinking": "Second thought."},
            {"type": "text", "text": "!"},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 8,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 5,
        },
    }


def make_request(**overrides: object) -> LLMRequest:
    return LLMRequest.model_validate(
        {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Hello"}],
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://provider.test",
        "https://provider.test/",
        "https://provider.test/v1",
        "https://provider.test/v1/",
        "https://provider.test/anthropic",
        "https://provider.test/anthropic/v1/",
    ],
)
def test_messages_request_and_response_conversion(
    base_url: str, message_response: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.adapters.anthropic.time", lambda: 1700000000)

    def handle(request: httpx.Request) -> httpx.Response:
        prefix = "/anthropic" if "/anthropic" in base_url else ""
        assert str(request.url) == f"https://provider.test{prefix}/v1/messages"
        assert request.method == "POST"
        assert request.headers["x-api-key"] == "message-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        assert request.extensions["timeout"]["read"] == 7
        assert json.loads(request.content) == {
            "model": "deepseek-v4-flash",
            "system": "First system\n\nSecond system",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
                {"role": "user", "content": "Continue"},
            ],
            "max_tokens": 128,
            "stream": False,
            "temperature": 0,
            "top_p": 0,
        }
        return httpx.Response(200, json=message_response)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            async with AnthropicMessagesAdapter(
                api_key=" message-key ", base_url=base_url, timeout=7, client=client
            ) as adapter:
                response = await adapter.complete(
                    make_request(
                        messages=[
                            {"role": "system", "content": "First system"},
                            {"role": "user", "content": "Hello"},
                            {"role": "system", "content": "Second system"},
                            {"role": "assistant", "content": "Hi"},
                            {"role": "user", "content": "Continue"},
                        ],
                        system_prompt={"name": "assistant", "version": "1.0.0"},
                        max_tokens=128,
                        temperature=0,
                        top_p=0,
                    )
                )
                assert response.model_dump() == {
                    "id": "msg-test",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Hello!",
                                "reasoning_content": "First thought.Second thought.",
                                "parsed": None,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
                    "structured_output": None,
                }
            assert not client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("refusal", "content_filter"),
        ("pause_turn", "pause_turn"),
        (None, None),
    ],
)
def test_defaults_plain_text_and_stop_reasons(
    message_response: dict, stop_reason: str | None, expected: str | None
) -> None:
    message_response.update(
        content=[{"type": "text", "text": "Hi"}],
        stop_reason=stop_reason,
        usage={"input_tokens": 1, "output_tokens": 2},
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1024,
            "stream": False,
        }
        return httpx.Response(200, json=message_response)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            async with AnthropicMessagesAdapter(api_key="key", client=client) as adapter:
                response = await adapter.complete(make_request())
                assert response.choices[0].finish_reason == expected
                assert "reasoning_content" not in response.choices[0].message.model_dump(
                    exclude_unset=True
                )
                assert response.usage.total_tokens == 3

    asyncio.run(run())


def test_system_only_request_is_rejected_before_transport() -> None:
    handler = Mock()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with AnthropicMessagesAdapter(api_key="key", client=client) as adapter:
                with pytest.raises(GatewayError) as error:
                    await adapter.complete(
                        make_request(messages=[{"role": "system", "content": "Instructions"}])
                    )
                assert error.value.code == "INVALID_MESSAGES"
        handler.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(("status", "expected"), [(200, True), (401, False), (404, False)])
def test_healthcheck_and_lifecycle(status: int, expected: bool) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.anthropic.com/v1/models"
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(status, json={"data": []})

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        async with AnthropicMessagesAdapter(api_key="key", client=client) as adapter:
            assert await adapter.healthcheck() is expected
            with pytest.raises(NotImplementedError, match="streaming"):
                adapter.stream(make_request())
        assert not client.is_closed
        await client.aclose()
        owned = AnthropicMessagesAdapter(api_key="key")
        await owned.aclose()
        assert owned._client.is_closed

    asyncio.run(run())


@pytest.fixture
def flash_client(
    model_configuration: dict,
    model_config_path: Path,
    upstream: Mock,
    message_response: dict,
    monkeypatch: pytest.MonkeyPatch,
    upstream_clients: list[httpx.AsyncClient],
) -> Iterator[TestClient]:
    model_configuration["models"].append(
        {
            "logical_name": "flash-chat",
            "provider": "anthropic",
            "model": "deepseek-v4-flash",
            "base_url": "https://flash.test/anthropic",
            "api_key_env": "TEST_FLASH_CHAT_API_KEY",
            "timeout_seconds": 9,
            "capabilities": {"reasoning": True, "max_output_tokens": 8192},
        }
    )
    model_config_path.write_text(json.dumps(model_configuration), encoding="utf-8")
    monkeypatch.setenv("TEST_FLASH_CHAT_API_KEY", "flash-only-key")
    upstream.return_value = httpx.Response(200, json=message_response)
    application = create_app(Settings(_env_file=None, models_config_path=model_config_path))
    with TestClient(application, raise_server_exceptions=False) as client:
        assert isinstance(
            application.state.model_registry.get_adapter("flash-chat"), AnthropicMessagesAdapter
        )
        yield client
    assert len(upstream_clients) == 1
    assert upstream_clients[0].is_closed


def test_flash_model_routing_template_and_catalog(flash_client: TestClient, upstream: Mock) -> None:
    response = flash_client.post(
        "/v1/chat/completions",
        json={
            "model": "flash-chat",
            "system_prompt": {
                "name": "assistant",
                "version": "1.0.0",
                "parameters": {"language": "English"},
            },
            "messages": [
                {"role": "system", "content": "Replace me"},
                {"role": "user", "content": "Hello"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["model"] == "flash-chat"
    assert response.json()["choices"][0]["message"]["content"] == "Hello!"
    request = upstream.call_args.args[0]
    assert str(request.url) == "https://flash.test/anthropic/v1/messages"
    assert request.headers["x-api-key"] == "flash-only-key"
    assert request.extensions["timeout"]["read"] == 9
    assert json.loads(request.content) == {
        "model": "deepseek-v4-flash",
        "system": "你是一名通用助手。请使用English回答用户的问题。",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 8192,
        "stream": False,
    }
    catalog = flash_client.get("/v1/models")
    assert catalog.status_code == 200
    flash = next(model for model in catalog.json()["data"] if model["id"] == "flash-chat")
    assert flash["capabilities"]["reasoning"] is True
    for private in ("deepseek-v4-flash", "flash.test", "TEST_FLASH_CHAT_API_KEY", "flash-only-key"):
        assert private not in catalog.text
        assert private not in response.text
    unavailable = flash_client.post(
        "/v1/chat/completions",
        json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
    )
    assert unavailable.status_code == 503
    upstream.assert_called_once()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_messages_http_errors_use_gateway_mapping(
    flash_client: TestClient, upstream: Mock, status: int
) -> None:
    upstream.return_value = httpx.Response(status, json={"error": {"message": "private-key"}})
    response = flash_client.post(
        "/v1/chat/completions", json=make_request(model="flash-chat").model_dump()
    )
    assert response.status_code == (429 if status == 429 else 502)
    assert response.json()["error"]["code"] == (
        "UPSTREAM_RATE_LIMITED" if status == 429 else "UPSTREAM_HTTP_ERROR"
    )
    assert "private-key" not in response.text


@pytest.mark.parametrize(
    ("error_class", "status", "code"),
    [
        (httpx.ReadTimeout, 504, "UPSTREAM_TIMEOUT"),
        (httpx.ConnectError, 502, "UPSTREAM_CONNECTION_ERROR"),
    ],
)
def test_messages_network_errors_use_gateway_mapping(
    flash_client: TestClient, upstream: Mock, error_class: type[Exception], status: int, code: str
) -> None:
    upstream.side_effect = error_class("private connection details")
    response = flash_client.post(
        "/v1/chat/completions", json=make_request(model="flash-chat").model_dump()
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "private connection details" not in response.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"content": [{"type": "tool_use", "id": "tool-1", "name": "search", "input": {}}]},
        {"content": [{"type": "text"}]},
        {"usage": {"input_tokens": -1, "output_tokens": 2}},
        {"usage": {"input_tokens": 1, "output_tokens": "2"}},
        {"type": "error"},
        {"content": None},
    ],
)
def test_malformed_messages_response_is_rejected(
    flash_client: TestClient, upstream: Mock, message_response: dict, overrides: dict
) -> None:
    upstream.return_value = httpx.Response(200, json={**message_response, **overrides})
    response = flash_client.post(
        "/v1/chat/completions", json=make_request(model="flash-chat").model_dump()
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_INVALID_RESPONSE"


def test_flash_token_limit_and_system_only_rejection(
    flash_client: TestClient, upstream: Mock
) -> None:
    for overrides, code in [
        ({"max_tokens": 8193}, "MODEL_TOKEN_LIMIT_EXCEEDED"),
        ({"messages": [{"role": "system", "content": "Instructions"}]}, "INVALID_MESSAGES"),
    ]:
        response = flash_client.post(
            "/v1/chat/completions", json=make_request(model="flash-chat", **overrides).model_dump()
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == code
    upstream.assert_not_called()
