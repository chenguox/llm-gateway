import asyncio
import json

import httpx2 as httpx
import pytest

from app.adapters import DeepSeekChatAdapter, ModelAdapter
from app.schemas.chat import ChatCompletionRequest, ChatMessage


def test_complete_sends_chat_request_and_parses_response() -> None:
    calls: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer unit-test-key"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "你好"},
            ],
            "temperature": 0.7,
            "max_tokens": 128,
            "stream": False,
        }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "你好！有什么可以帮助你？"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
            async with DeepSeekChatAdapter(api_key="unit-test-key", client=client) as deepseek:
                adapter: ModelAdapter = deepseek
                response = await adapter.complete(
                    ChatCompletionRequest(
                        model="deepseek-chat",
                        messages=[
                            ChatMessage(role="system", content="You are a helpful assistant."),
                            ChatMessage(role="user", content="你好"),
                        ],
                        temperature=0.7,
                        max_tokens=128,
                    )
                )

                assert response.id == "chatcmpl-test"
                assert response.model == "deepseek-chat"
                assert response.choices[0].message.content == "你好！有什么可以帮助你？"
                assert response.choices[0].finish_reason == "stop"
                assert response.usage is not None
                assert response.usage.total_tokens == 20
            assert not client.is_closed

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_complete_propagates_upstream_http_errors(status_code: int) -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "Upstream failure"}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
            async with DeepSeekChatAdapter(api_key="unit-test-key", client=client) as adapter:
                with pytest.raises(httpx.HTTPStatusError) as exc_info:
                    await adapter.complete(
                        ChatCompletionRequest(
                            model="deepseek-chat",
                            messages=[ChatMessage(role="user", content="Hello")],
                        )
                    )
                assert exc_info.value.response.status_code == status_code

    asyncio.run(run())


@pytest.mark.parametrize(("status_code", "expected"), [(200, True), (401, False)])
def test_healthcheck_checks_authenticated_models_access(status_code: int, expected: bool) -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.deepseek.com/models"
        assert request.headers["authorization"] == "Bearer unit-test-key"
        return httpx.Response(status_code, json={"data": []})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
            async with DeepSeekChatAdapter(api_key="unit-test-key", client=client) as adapter:
                assert await adapter.healthcheck() is expected

    asyncio.run(run())
