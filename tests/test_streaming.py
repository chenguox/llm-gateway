"""Exercise the public SSE protocol using real adapters and fragmented upstream bytes."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from unittest.mock import Mock

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.anthropic import AnthropicMessagesAdapter
from app.adapters.compatible import ChatCompletionsAdapter
from app.adapters.deepseek import DeepSeekChatAdapter
from app.schemas.chat import LLMRequest
from app.streaming.provider import CompletionStream
from app.streaming.service import GatewayStreamingResponse, StreamSession


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes, *, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.closed = False
        self.reads = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.content), 7):
            self.reads += 1
            yield self.content[offset : offset + 7]
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


def sse(data: dict | str, event: str | None = None) -> bytes:
    value = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {value}\n\n".encode()


def frame(
    content: str | None = None,
    *,
    delta: dict | None = None,
    finish: str | None = None,
    choices: list | None = None,
    usage: dict | None = None,
) -> dict:
    return {
        "id": "provider-private-id",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "provider-private-model",
        "choices": choices
        if choices is not None
        else [
            {
                "index": 0,
                "delta": delta if delta is not None else {"content": content},
                "finish_reason": finish,
            }
        ],
        "usage": usage,
    }


def response_stream(content: bytes, **kwargs: object) -> tuple[httpx.Response, FragmentedStream]:
    body = FragmentedStream(content, **kwargs)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body), body


def events(response: httpx.Response) -> list[dict]:
    result = []
    stream_id = None
    for sequence, record in enumerate(response.text.strip().split("\n\n"), 1):
        fields = dict(line.split(": ", 1) for line in record.splitlines())
        payload = json.loads(fields["data"])
        stream_id = stream_id or payload["stream_id"]
        assert payload["stream_id"] == stream_id
        assert payload["sequence"] == sequence
        assert fields["id"] == f"{stream_id}:{sequence}"
        assert fields["event"] == payload["type"]
        result.append(payload)
    assert result[0]["type"] == "started"
    terminals = [event for event in result if event["type"] in {"completed", "failed", "cancelled"}]
    assert len(terminals) == 1
    assert terminals[0] == result[-1]
    return result


@pytest.fixture
def streaming_client(
    model_configuration: dict, request: pytest.FixtureRequest
) -> Iterator[TestClient]:
    for model in model_configuration["models"]:
        model["capabilities"]["streaming"] = True
    application = request.getfixturevalue("chat_application")
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def stream_request(model: str = "general-chat", **overrides: object) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
        **overrides,
    }


@pytest.mark.parametrize("model", ["general-chat", "coding-assistant"])
def test_stream_normalizes_deltas_usage_and_late_terminal(
    streaming_client: TestClient, upstream: Mock, model: str
) -> None:
    usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    content = (
        b"\xef\xbb\xbf: keepalive\r\n\r\n"
        + sse(frame(delta={"role": "assistant", "reasoning_content": "Think"}))
        + sse(frame("你好\nworld"))
        + sse(frame(finish="stop"))
        + sse(frame(choices=[], usage=usage))
        + sse("[DONE]")
        + sse("[DONE]")
        + sse("invalid trailing data")
    )
    upstream.return_value, body = response_stream(content)

    response = streaming_client.post("/v1/chat/completions", json=stream_request(model))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    result = events(response)
    assert [event["type"] for event in result] == [
        "started",
        "delta",
        "delta",
        "usage",
        "completed",
    ]
    assert result[1]["delta"] == {"index": 0, "role": "assistant", "reasoning_content": "Think"}
    assert result[2]["delta"]["content"] == "你好\nworld"
    assert result[-1]["usage"] == usage
    assert result[-1]["finish_reasons"] == [{"index": 0, "finish_reason": "stop"}]
    assert all(event["model"] == model for event in result)
    assert "provider-private" not in response.text
    assert body.closed
    upstream.assert_called_once()
    request = upstream.call_args.args[0]
    payload = json.loads(request.content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert request.headers["accept"] == "text/event-stream"
    if model == "coding-assistant":
        assert payload["max_tokens"] == 256


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        (b"", "UPSTREAM_INVALID_RESPONSE"),
        (sse(frame(finish="stop")), "UPSTREAM_INVALID_RESPONSE"),
        (sse("[DONE]"), "UPSTREAM_INVALID_RESPONSE"),
        (sse("invalid JSON private-secret"), "UPSTREAM_INVALID_RESPONSE"),
        (sse({"error": {"message": "private-secret"}}), "UPSTREAM_STREAM_ERROR"),
        (sse(frame(delta={"tool_calls": [{"id": "private-secret"}]})), "UPSTREAM_INVALID_RESPONSE"),
        (sse({"choices": []}), "UPSTREAM_INVALID_RESPONSE"),
        (b'data: {"private-secret": true}', "UPSTREAM_INVALID_RESPONSE"),
    ],
)
def test_stream_failures_have_one_sanitized_terminal(
    streaming_client: TestClient, upstream: Mock, tail: bytes, code: str
) -> None:
    upstream.return_value, body = response_stream(sse(frame("partial")) + tail)

    response = streaming_client.post("/v1/chat/completions", json=stream_request())

    assert response.status_code == 200
    result = events(response)
    assert result[1]["delta"]["content"] == "partial"
    assert result[-1]["type"] == "failed"
    assert result[-1]["error"]["code"] == code
    assert "private-secret" not in response.text
    assert body.closed
    upstream.assert_called_once()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("private-secret"), "UPSTREAM_TIMEOUT"),
        (httpx.ReadError("private-secret"), "UPSTREAM_CONNECTION_ERROR"),
        (RuntimeError("private-secret"), "INTERNAL_ERROR"),
    ],
)
def test_midstream_transport_errors(
    streaming_client: TestClient, upstream: Mock, error: Exception, code: str
) -> None:
    upstream.return_value, body = response_stream(sse(frame("partial")), error=error)

    response = streaming_client.post("/v1/chat/completions", json=stream_request())

    result = events(response)
    assert result[-1]["error"]["code"] == code
    assert "private-secret" not in response.text
    assert body.closed
    upstream.assert_called_once()


@pytest.mark.parametrize(
    ("status", "code"), [(429, "UPSTREAM_RATE_LIMITED"), (503, "UPSTREAM_HTTP_ERROR")]
)
def test_stream_http_errors_remain_json_before_sse(
    streaming_client: TestClient, upstream: Mock, status: int, code: str
) -> None:
    body = FragmentedStream(b"private-secret")
    upstream.return_value = httpx.Response(status, stream=body)

    response = streaming_client.post("/v1/chat/completions", json=stream_request())

    assert response.status_code == (429 if status == 429 else 502)
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == code
    assert body.reads == 0
    assert body.closed


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ConnectError])
def test_stream_connection_errors_remain_json(
    streaming_client: TestClient, upstream: Mock, error: type[Exception]
) -> None:
    upstream.side_effect = error("private-secret")

    response = streaming_client.post("/v1/chat/completions", json=stream_request())

    assert response.status_code == (504 if error is httpx.ConnectTimeout else 502)
    assert response.headers["content-type"] == "application/json"
    assert "private-secret" not in response.text


def test_wrong_stream_content_type_is_rejected_before_sse(
    streaming_client: TestClient, upstream: Mock
) -> None:
    body = FragmentedStream(b"private-secret")
    upstream.return_value = httpx.Response(
        200, headers={"content-type": "application/json"}, stream=body
    )

    response = streaming_client.post("/v1/chat/completions", json=stream_request())

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_INVALID_RESPONSE"
    assert body.closed
    assert body.reads == 0


def test_streaming_disabled_is_rejected(chat_client: TestClient, upstream: Mock) -> None:
    response = chat_client.post("/v1/chat/completions", json=stream_request())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CAPABILITY_NOT_SUPPORTED"
    upstream.assert_not_called()


@pytest.mark.parametrize(
    "output_format",
    [
        {"type": "json_object"},
        {
            "type": "json_schema",
            "json_schema": {"name": "test", "schema": {"type": "object", "properties": {}}},
        },
    ],
)
def test_streaming_structured_output_is_rejected_without_calling_provider(
    streaming_client: TestClient, upstream: Mock, output_format: dict
) -> None:
    response = streaming_client.post(
        "/v1/chat/completions", json=stream_request(response_format=output_format)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "STREAMING_STRUCTURED_OUTPUT_NOT_SUPPORTED"
    upstream.assert_not_called()


@pytest.mark.parametrize("adapter_class", [ChatCompletionsAdapter, DeepSeekChatAdapter])
def test_complete_opens_stream_without_buffering(adapter_class: type) -> None:
    async def run() -> None:
        response, body = response_stream(sse(frame("Hello", finish="stop")) + sse("[DONE]"))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as client:
            adapter = adapter_class(api_key="key", base_url="https://provider.test", client=client)
            result = await adapter.complete(LLMRequest.model_validate(stream_request()))
            assert isinstance(result, CompletionStream)
            assert body.reads == 0
            updates = [update async for update in result]
            assert [update.type for update in updates] == ["delta", "completed"]
            await result.aclose()
            assert body.closed
            assert not client.is_closed

    asyncio.run(run())


def test_sse_multiline_data_and_keepalive(streaming_client: TestClient, upstream: Mock) -> None:
    value = json.dumps(frame("Hi", finish="stop"), indent=2)
    multiline = "event: message\r\n" + "\r\n".join(f"data: {line}" for line in value.splitlines())
    content = b": heartbeat\r\n\r\n" + (multiline + "\r\n\r\n").encode() + sse("[DONE]")
    upstream.return_value, body = response_stream(content)

    response = streaming_client.post("/v1/chat/completions", json=stream_request())

    assert events(response)[-1]["type"] == "completed"
    assert body.closed


def test_multiple_choices_finish_before_completion(
    streaming_client: TestClient, upstream: Mock
) -> None:
    choices = [
        {"index": 1, "delta": {"content": "second"}, "finish_reason": "length"},
        {"index": 0, "delta": {"content": "first"}, "finish_reason": "stop"},
    ]
    upstream.return_value, body = response_stream(sse(frame(choices=choices)) + sse("[DONE]"))

    result = events(streaming_client.post("/v1/chat/completions", json=stream_request()))

    assert [event["delta"]["index"] for event in result if "delta" in event] == [1, 0]
    assert result[-1]["finish_reasons"] == [
        {"index": 0, "finish_reason": "stop"},
        {"index": 1, "finish_reason": "length"},
    ]
    assert "usage" not in result[-1]
    assert body.closed


def message_events() -> list[dict]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "provider-private-id",
                "type": "message",
                "role": "assistant",
                "model": "provider-private-model",
                "content": [],
                "stop_reason": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                },
            },
        },
        {"type": "ping"},
        {"type": "new_provider_event", "private": "private-secret"},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Think"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "private-secret"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "private-secret"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "text", "text": "你好"},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": "\nworld"},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 8},
        },
        {"type": "message_stop"},
    ]


@pytest.fixture
def messages_client(
    model_configuration: dict, request: pytest.FixtureRequest
) -> Iterator[TestClient]:
    model = model_configuration["models"][0]
    model["provider"] = "anthropic"
    model["capabilities"]["streaming"] = True
    application = request.getfixturevalue("chat_application")
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def test_messages_normalize_reasoning_cached_usage_and_finish(
    messages_client: TestClient, upstream: Mock
) -> None:
    content = b"".join(sse(event, event["type"]) for event in message_events())
    upstream.return_value, body = response_stream(content + sse({"type": "error"}, "error"))
    payload = stream_request(system_prompt={"name": "assistant", "version": "1.0.0"})

    response = messages_client.post("/v1/chat/completions", json=payload)

    result = events(response)
    assert result[-1]["type"] == "completed"
    deltas = [event["delta"] for event in result if event["type"] == "delta"]
    assert deltas == [
        {"index": 0, "role": "assistant"},
        {"index": 0, "reasoning_content": "Think"},
        {"index": 0, "content": "你好"},
        {"index": 0, "content": "\nworld"},
    ]
    assert result[-1]["usage"] == {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23}
    assert result[-1]["finish_reasons"] == [{"index": 0, "finish_reason": "stop"}]
    assert "private-secret" not in response.text
    assert "provider-private" not in response.text
    assert body.closed
    request = upstream.call_args.args[0]
    body_json = json.loads(request.content)
    assert str(request.url) == "https://provider.test/v1/messages"
    assert request.headers["x-api-key"] == "unit-test-key"
    assert body_json["stream"] is True
    assert "system" in body_json
    assert "system_prompt" not in body_json
    assert "stream_options" not in body_json
    upstream.assert_called_once()


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        ([], "UPSTREAM_INVALID_RESPONSE"),
        ([{"type": "message_stop"}], "UPSTREAM_INVALID_RESPONSE"),
        (
            [{"type": "error", "error": {"type": "overloaded_error", "message": "private-secret"}}],
            "UPSTREAM_STREAM_ERROR",
        ),
        (
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "private-secret"},
                }
            ],
            "UPSTREAM_INVALID_RESPONSE",
        ),
        (
            [
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "private-secret"},
                }
            ],
            "UPSTREAM_INVALID_RESPONSE",
        ),
        ([{"type": "message_start", "message": {}}], "UPSTREAM_INVALID_RESPONSE"),
    ],
)
def test_messages_failures_have_one_terminal(
    messages_client: TestClient, upstream: Mock, tail: list[dict], code: str
) -> None:
    content = b"".join(sse(event, event["type"]) for event in [message_events()[0], *tail])
    upstream.return_value, body = response_stream(content)

    response = messages_client.post("/v1/chat/completions", json=stream_request())

    result = events(response)
    assert result[-1]["type"] == "failed"
    assert result[-1]["error"]["code"] == code
    assert "private-secret" not in response.text
    assert body.closed


@pytest.mark.parametrize("reason", ["end_turn", "max_tokens", "refusal"])
def test_messages_complete_uses_same_entry_point(reason: str) -> None:
    async def run() -> None:
        upstream_events = message_events()
        upstream_events[-2]["delta"]["stop_reason"] = reason
        content = b"".join(sse(event, event["type"]) for event in upstream_events)
        response, body = response_stream(content)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as client:
            adapter = AnthropicMessagesAdapter(api_key="key", client=client)
            result = await adapter.complete(LLMRequest.model_validate(stream_request()))
            assert isinstance(result, CompletionStream)
            assert body.reads == 0
            updates = [update async for update in result]
            expected = {"end_turn": "stop", "max_tokens": "length", "refusal": "content_filter"}
            assert updates[-1].finish_reasons[0].finish_reason == expected[reason]
            await result.aclose()
            assert body.closed
            assert not client.is_closed

    asyncio.run(run())


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse(frame("first"))
        self.waiting.set()
        await self.release.wait()
        yield sse(frame("last", finish="stop")) + sse("[DONE]")

    async def aclose(self) -> None:
        self.closes += 1


async def waiting_session(body: WaitingStream) -> tuple[StreamSession, httpx.AsyncClient]:
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    adapter = ChatCompletionsAdapter(api_key="key", base_url="https://provider.test", client=client)
    upstream = await adapter.complete(LLMRequest.model_validate(stream_request()))
    return StreamSession(upstream, "general-chat"), client


def test_cancellation_while_reading_closes_upstream_and_has_one_terminal() -> None:
    async def run() -> None:
        body = WaitingStream()
        session, client = await waiting_session(body)
        output = session.events()
        assert (await anext(output)).type == "started"
        assert (await anext(output)).delta.content == "first"
        pending = asyncio.create_task(anext(output))
        await asyncio.wait_for(body.waiting.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        terminal = session.terminal
        assert terminal.type == "cancelled"
        assert session.cancel() is None
        await output.aclose()
        await session.aclose()
        assert session.terminal is terminal
        assert body.closes == 1
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("fail_at", ["headers", "started", "delta"])
def test_downstream_write_failure_closes_even_before_iteration(
    spec_version: str, fail_at: str
) -> None:
    async def run() -> None:
        body = WaitingStream()
        session, client = await waiting_session(body)
        response = GatewayStreamingResponse(session)

        async def send(message: dict) -> None:
            if fail_at == "headers" and message["type"] == "http.response.start":
                raise OSError("client disconnected")
            if message["type"] == "http.response.body":
                if f"event: {fail_at}".encode() in message["body"]:
                    raise OSError("client disconnected")

        async def receive() -> dict:
            await asyncio.Event().wait()

        scope = {"type": "http", "asgi": {"spec_version": spec_version}}
        try:
            await asyncio.wait_for(response(scope, receive, send), 1)
        except (OSError, ExceptionGroup):
            pass
        assert session.terminal.type == "cancelled"
        assert body.closes == 1
        assert session.cancel() is None
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
def test_disconnect_while_waiting_cancels_upstream(spec_version: str) -> None:
    async def run() -> None:
        body = WaitingStream()
        session, client = await waiting_session(body)
        response = GatewayStreamingResponse(session)
        sent = []

        async def send(message: dict) -> None:
            sent.append(message)

        async def receive() -> dict:
            await body.waiting.wait()
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": spec_version}}
        await asyncio.wait_for(response(scope, receive, send), 1)
        assert session.terminal.type == "cancelled"
        assert body.closes == 1
        assert any(b"first" in message.get("body", b"") for message in sent)
        assert not any(b"event: completed" in message.get("body", b"") for message in sent)
        await client.aclose()

    asyncio.run(run())


def test_completed_wins_over_late_cancellation() -> None:
    async def run() -> None:
        body = WaitingStream()
        body.release.set()
        session, client = await waiting_session(body)
        output = [event async for event in session.events()]
        assert [event.type for event in output] == ["started", "delta", "delta", "completed"]
        terminal = session.terminal
        assert session.cancel() is None
        assert session.terminal is terminal
        assert terminal.type == "completed"
        assert body.closes == 1
        await client.aclose()

    asyncio.run(run())


def test_failure_wins_over_late_cancellation() -> None:
    async def run() -> None:
        response, body = response_stream(sse(frame("partial")), error=httpx.ReadTimeout("private"))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as client:
            adapter = ChatCompletionsAdapter(
                api_key="key", base_url="https://provider.test", client=client
            )
            upstream = await adapter.complete(LLMRequest.model_validate(stream_request()))
            session = StreamSession(upstream, "general-chat")
            output = [event async for event in session.events()]
            assert [event.type for event in output] == ["started", "delta", "failed"]
            assert session.cancel() is None
            assert session.terminal is output[-1]
            assert body.closed

    asyncio.run(run())
