"""Call accounting, confidentiality, pricing and durable terminal-state tests."""

import asyncio
import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import anyio
import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_streaming import frame, response_stream, sse

from app.core.config import Settings
from app.governance.models import InvocationRecord, Timing
from app.governance.pricing import Price, PriceBook, Prices, aggregate_cost, estimate
from app.governance.store import RecordStorageError, RecordStore
from app.schemas.chat import ChatCompletionUsage, LLMRequest
from app.usage import compatible_usage


def request_body(**overrides: object) -> dict:
    return {
        "model": "general-chat",
        "messages": [{"role": "user", "content": "secret-customer-message"}],
        **overrides,
    }


def price_data(**overrides: object) -> dict:
    return {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "version": "test-v1",
        "effective_at": "2020-01-01T00:00:00Z",
        "currency": "USD",
        "input_per_million": "1",
        "output_per_million": "2",
        **overrides,
    }


@pytest.fixture
def priced_client(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_configuration: dict,
) -> TestClient:
    model_configuration["models"][0]["capabilities"]["streaming"] = True
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"prices": [price_data()]}), encoding="utf-8")
    monkeypatch.setenv("PRICES_CONFIG_PATH", str(path))
    return request.getfixturevalue("chat_client")


def read_records(client: TestClient, response) -> tuple[dict, list[dict]]:
    async def read() -> tuple[dict, list[dict]]:
        calls = await client.app.state.record_store.find_calls(
            request_id=response.headers["x-request-id"]
        )
        assert len(calls) == 1
        call = calls[0]
        return call, await client.app.state.record_store.find_attempts(call["call_id"])

    return asyncio.run(read())


def test_success_records_all_groups_without_content(
    priced_client: TestClient, upstream: Mock, completion: dict, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app")
    completion["choices"][0]["message"].update(
        content="secret-answer", reasoning_content="secret-thought"
    )
    upstream.return_value = httpx.Response(
        200, json=completion, headers={"x-request-id": "provider-123"}
    )

    response = priced_client.post(
        "/v1/chat/completions",
        json=request_body(),
        headers={
            "Authorization": "Bearer secret-caller-key",
            "X-Request-ID": "secret-untrusted-id",
        },
    )

    assert response.status_code == 200
    assert response.json()["usage"] == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    call, attempts = read_records(priced_client, response)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert call["request_id"] != "secret-untrusted-id"
    assert attempt["call_id"] == call["id"]
    assert attempt["upstream_request_id"] == "provider-123"
    assert call["route"] == {
        "logical_model": "general-chat",
        "provider": "deepseek",
        "provider_model": "deepseek-chat",
    }
    assert call["prompt"]["source"] == "inline"
    assert call["prompt"]["message_count"] == 1
    assert call["prompt"]["fingerprint"] is None
    assert call["usage"]["status"] == "complete"
    assert call["usage"]["input_tokens"] == 12
    assert call["resilience"]["attempt_count"] == 1
    assert call["result"]["status"] == "completed"
    assert call["result"]["http_status"] == 200
    assert call["error"] is None
    assert call["timing"]["duration_ms"] >= attempt["timing"]["duration_ms"] >= 0
    assert call["timing"]["first_content_ms"] is None
    assert Decimal(call["cost"]["amount"]) == Decimal("0.000028")
    assert attempt["cost"]["price_snapshot"]["version"] == "test-v1"
    assert call["cost"]["status"] == "estimated"
    serialized = json.dumps([call, attempts]) + caplog.text
    for secret in (
        "secret-customer-message",
        "secret-answer",
        "secret-thought",
        "secret-caller-key",
        "secret-untrusted-id",
        "unit-test-key",
    ):
        assert secret not in serialized


@pytest.mark.parametrize(
    ("overrides", "code", "stage"),
    [
        ({"messages": []}, "VALIDATION_ERROR", "validation"),
        ({"model": "secret-unknown-model"}, "MODEL_NOT_FOUND", "routing"),
        (
            {"system_prompt": {"name": "secret-unknown-prompt", "version": "1.0"}},
            "PROMPT_TEMPLATE_NOT_FOUND",
            "prompt",
        ),
    ],
)
def test_rejected_requests_are_recorded_without_upstream_attempts(
    priced_client: TestClient,
    upstream: Mock,
    overrides: dict,
    code: str,
    stage: str,
) -> None:
    response = priced_client.post("/v1/chat/completions", json=request_body(**overrides))

    call, attempts = read_records(priced_client, response)
    assert call["result"]["status"] == "failed"
    assert call["error"]["code"] == code
    assert call["error"]["stage"] == stage
    assert attempts == []
    assert call["cost"]["status"] == "not_incurred"
    assert Decimal(call["cost"]["amount"]) == 0
    assert "secret-unknown" not in json.dumps(call)
    upstream.assert_not_called()


def test_repair_attempts_are_charged_once_and_usage_is_aggregated(
    priced_client: TestClient,
    upstream: Mock,
    completion: dict,
) -> None:
    invalid, valid = copy.deepcopy(completion), copy.deepcopy(completion)
    invalid["choices"][0]["message"]["content"] = "secret-invalid-json"
    valid["choices"][0]["message"]["content"] = '{"ok":true}'
    upstream.side_effect = [httpx.Response(200, json=invalid), httpx.Response(200, json=valid)]

    response = priced_client.post(
        "/v1/chat/completions", json=request_body(response_format={"type": "json_object"})
    )

    assert response.status_code == 200
    assert response.json()["usage"] == {"input_tokens": 24, "output_tokens": 16, "total_tokens": 40}
    assert "total_usage" not in response.json()["structured_output"]
    call, attempts = read_records(priced_client, response)
    assert len(attempts) == 2
    assert call["usage"]["input_tokens"] == 24
    assert call["resilience"]["repair_retry_count"] == 1
    assert call["result"]["validation_status"] == "passed"
    assert attempts[0]["result"]["validation_status"] == "failed"
    assert attempts[0]["error"]["stage"] == "structured_validation"
    assert attempts[1]["result"]["validation_status"] == "passed"
    assert attempts[1]["resilience"]["retry_reason"] == "structured_output_validation"
    assert Decimal(call["cost"]["amount"]) == Decimal("0.000056")
    assert "secret-invalid-json" not in json.dumps(attempts)


def test_later_network_failure_preserves_previous_attempt_cost(
    priced_client: TestClient,
    upstream: Mock,
    completion: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid = copy.deepcopy(completion)
    invalid["choices"][0]["message"]["content"] = "invalid"
    upstream.side_effect = [
        httpx.Response(200, json=invalid),
        httpx.ReadTimeout("secret-provider-error"),
    ]

    response = priced_client.post(
        "/v1/chat/completions", json=request_body(response_format={"type": "json_object"})
    )

    assert response.status_code == 504
    call, attempts = read_records(priced_client, response)
    assert call["result"]["status"] == "failed"
    assert call["usage"]["status"] == "partial"
    assert call["usage"]["input_tokens"] is None
    assert call["usage"]["known_input_tokens"] == 12
    assert call["resilience"]["timeout_count"] == 1
    assert call["cost"]["amount"] is None
    assert Decimal(call["cost"]["known_amount"]) == Decimal("0.000028")
    assert call["result"]["reconciliation_required"] is True
    assert attempts[1]["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert "secret-provider-error" not in json.dumps([call, attempts]) + caplog.text


def test_prompt_hmac_is_keyed_and_tracks_actual_repair_prompt(
    priced_client: TestClient,
    upstream: Mock,
    completion: dict,
) -> None:
    priced_client.app.state.settings.audit_hmac_key = SecretStr(
        "test-hmac-secret-key-32-bytes-long"
    )
    invalid, valid = copy.deepcopy(completion), copy.deepcopy(completion)
    invalid["choices"][0]["message"]["content"] = "secret-bad-json"
    valid["choices"][0]["message"]["content"] = "{}"
    upstream.side_effect = [httpx.Response(200, json=invalid), httpx.Response(200, json=valid)]

    response = priced_client.post(
        "/v1/chat/completions",
        json=request_body(
            system_prompt={"name": "assistant", "version": "1.0.0"},
            response_format={"type": "json_object"},
        ),
    )

    call, attempts = read_records(priced_client, response)
    assert call["prompt"]["source"] == "template"
    assert call["prompt"]["name"] == "assistant"
    assert call["prompt"]["version"] == "1.0.0"
    assert len(call["prompt"]["fingerprint"]) == 64
    assert call["prompt"]["fingerprint_key_id"] == "v1"
    assert attempts[0]["prompt"]["fingerprint"] != attempts[1]["prompt"]["fingerprint"]
    assert attempts[1]["prompt"]["message_count"] > attempts[0]["prompt"]["message_count"]
    assert "test-hmac-secret" not in json.dumps([call, attempts])


@pytest.mark.parametrize("fail_at", [1, 2])
def test_record_start_failure_prevents_provider_call(
    priced_client: TestClient,
    upstream: Mock,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: int,
) -> None:
    store = priced_client.app.state.record_store
    create = store.create
    count = 0

    async def fail(record: InvocationRecord) -> None:
        nonlocal count
        count += 1
        if count == fail_at:
            raise RecordStorageError("secret-storage-error")
        await create(record)

    monkeypatch.setattr(store, "create", fail)
    response = priced_client.post("/v1/chat/completions", json=request_body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CALL_RECORD_UNAVAILABLE"
    assert "secret-storage-error" not in response.text
    upstream.assert_not_called()
    if fail_at == 2:
        call, attempts = read_records(priced_client, response)
        assert call["result"]["status"] == "failed"
        assert call["cost"]["status"] == "not_incurred"
        assert attempts == []


def test_finish_storage_failure_is_visible_and_leaves_pending_record(
    priced_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = priced_client.app.state.record_store
    save = store.save

    async def fail_terminal(record: InvocationRecord) -> None:
        if record.result.status != "in_progress":
            raise RecordStorageError("secret-storage-error")
        await save(record)

    monkeypatch.setattr(store, "save", fail_terminal)

    response = priced_client.post("/v1/chat/completions", json=request_body())

    assert response.status_code == 200
    call, attempts = read_records(priced_client, response)
    assert call["result"]["status"] == "in_progress"
    assert call["result"]["reconciliation_required"] is True
    assert attempts[0]["result"]["status"] == "in_progress"
    assert "CALL_RECORD_WRITE_FAILED" in caplog.text
    assert "secret-storage-error" not in caplog.text


def test_unknown_usage_and_unknown_prices_never_report_zero_cost(
    priced_client: TestClient,
    upstream: Mock,
    completion: dict,
) -> None:
    completion.pop("usage")
    upstream.return_value = httpx.Response(200, json=completion)
    response = priced_client.post("/v1/chat/completions", json=request_body())
    call, _ = read_records(priced_client, response)
    assert call["usage"]["status"] == "missing"
    assert call["cost"]["status"] == "partial"
    assert call["cost"]["amount"] is None


def test_concurrent_calls_keep_independent_ids_and_costs(priced_client: TestClient) -> None:
    def send(number: int):
        return priced_client.post("/v1/chat/completions", json=request_body())

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(send, range(8)))
    assert all(response.status_code == 200 for response in responses)
    ids = [response.headers["x-request-id"] for response in responses]
    assert len(set(ids)) == 8
    for response in responses:
        call, attempts = read_records(priced_client, response)
        assert len(attempts) == 1
        assert call["cost"]["amount"] == "0.000028"


def test_cache_and_reasoning_are_not_double_charged() -> None:
    price = Price.model_validate(
        price_data(
            input_pricing="cache_split",
            cache_read_per_million="0.1",
            cache_creation_per_million="1.5",
        )
    )
    usage = compatible_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 6},
            "total_tokens": 999,
            "secret-extra-usage": "secret-value",
        }
    )
    assert usage.model_dump() == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    cost = estimate(price, usage, True)
    assert cost.status == "estimated"
    assert cost.amount == Decimal("0.000122")
    assert "secret" not in cost.model_dump_json()


def test_cache_pricing_needs_breakdown_and_rates() -> None:
    price = Price.model_validate(
        price_data(input_pricing="cache_split", cache_read_per_million="0.1")
    )
    missing = estimate(
        price, compatible_usage({"prompt_tokens": 100, "completion_tokens": 20}), True
    )
    assert missing.status == "partial"
    assert missing.amount is None
    assert missing.known_amount == Decimal("0.000040")
    usage = ChatCompletionUsage(input_tokens=100, output_tokens=20)
    usage._details = {"cache_read_input_tokens": 20, "cache_creation_input_tokens": 10}
    partial = estimate(price, usage, True)
    assert partial.amount is None
    assert partial.known_amount == Decimal("0.000112")


def test_price_version_effective_date_and_currency_are_preserved() -> None:
    old = price_data()
    future = price_data(version="v2", effective_at="2099-01-01T00:00:00Z", input_per_million="9")
    book = PriceBook(Prices.model_validate({"prices": [future, old]}))
    selected = book.select("deepseek", "deepseek-chat", datetime(2026, 11, 5, tzinfo=UTC))
    usd = estimate(selected, ChatCompletionUsage(input_tokens=100, output_tokens=20), True)
    assert usd.price_snapshot.version == "test-v1"
    assert usd.amount == Decimal("0.00014")
    eur = estimate(
        Price.model_validate(price_data(currency="EUR")),
        ChatCompletionUsage(input_tokens=100, output_tokens=20),
        True,
    )
    mixed = aggregate_cost([usd, eur])
    assert mixed.amount is None
    assert mixed.currency is None
    assert mixed.by_currency == {"USD": Decimal("0.00014"), "EUR": Decimal("0.00014")}
    assert estimate(None, ChatCompletionUsage(input_tokens=0, output_tokens=0), True).amount is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_per_million": "-1"},
        {"output_per_million": "NaN"},
        {"effective_at": "2026-01-01T00:00:00"},
        {"currency": "usd"},
        {"cache_read_per_million": "1"},
    ],
)
def test_invalid_prices_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Price.model_validate(price_data(**overrides))


def test_hmac_key_validation_does_not_expose_secret() -> None:
    assert Settings(_env_file=None, audit_hmac_key="").audit_hmac_key is None
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, audit_hmac_key="secret-short-key")
    assert "secret-short-key" not in str(error.value)


def test_retention_and_terminal_updates(tmp_path: Path) -> None:
    async def run() -> None:
        store = RecordStore(tmp_path / "records.sqlite3")
        await store.initialize(30)
        old = datetime.now(UTC) - timedelta(days=40)
        record = InvocationRecord(
            id="old", call_id="old", request_id="old", timing=Timing(started_at=old)
        )
        await store.create(record)
        attempt = record.model_copy(deep=True, update={"id": "attempt-old", "attempt_number": 1})
        await store.create(attempt)
        record.result.status = "completed"
        record.timing.ended_at = old
        await store.save(record)
        pending = InvocationRecord(
            id="pending", call_id="pending", request_id="pending", timing=Timing(started_at=old)
        )
        await store.create(pending)
        await store.initialize(30)
        remaining = await store.find_calls()
        assert [row["id"] for row in remaining] == ["pending"]
        assert await store.find_attempts("old") == []
        complete = pending.model_copy(deep=True)
        complete.result.status = "completed"
        cancel = pending.model_copy(deep=True)
        cancel.result.status = "cancelled"
        await asyncio.gather(store.save(complete), store.save(cancel))
        saved = (await store.find_calls(request_id="pending"))[0]
        assert saved["result"]["status"] in {"completed", "cancelled"}
        loser = cancel if saved["result"]["status"] == "completed" else complete
        await store.save(loser)
        assert (await store.find_calls(request_id="pending"))[0] == saved

    asyncio.run(run())


@pytest.mark.parametrize("fail", [False, True])
def test_stream_snapshots_are_not_added_and_final_result_is_recorded(
    priced_client: TestClient,
    upstream: Mock,
    fail: bool,
) -> None:
    first_usage = {"prompt_tokens": 12, "completion_tokens": 1}
    final_usage = {"prompt_tokens": 12, "completion_tokens": 8}
    content = (
        sse(frame(delta={"role": "assistant"}, usage=first_usage))
        + sse(frame("secret-stream-answer"))
        + sse(frame(finish="stop"))
        + sse(frame(choices=[], usage=final_usage))
    )
    if not fail:
        content += sse("[DONE]")
    upstream.return_value, body = response_stream(content)

    response = priced_client.post("/v1/chat/completions", json=request_body(stream=True))

    call, attempts = read_records(priced_client, response)
    assert response.status_code == 200
    assert call["result"]["status"] == ("failed" if fail else "completed")
    assert call["result"]["http_status"] == 200
    assert attempts[0]["usage"]["input_tokens"] == 12
    assert attempts[0]["usage"]["output_tokens"] == 8
    assert call["usage"]["known_input_tokens"] == 12
    assert call["usage"]["known_output_tokens"] == 8
    assert call["usage"]["status"] == ("partial" if fail else "complete")
    assert call["cost"]["amount"] == (None if fail else "0.000028")
    assert call["stream_id"] == attempts[0]["stream_id"]
    assert call["timing"]["first_content_ms"] >= 0
    assert attempts[0]["timing"]["first_content_ms"] >= 0
    assert "secret-stream-answer" not in json.dumps([call, attempts])
    assert body.closed


def test_disconnect_persists_cancelled_usage_and_releases_provider(
    priced_client: TestClient,
    upstream: Mock,
) -> None:
    async def run() -> None:
        class WaitingBody(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self.waiting = asyncio.Event()
                self.closes = 0

            async def __aiter__(self):
                yield sse(frame("first", usage={"prompt_tokens": 12, "completion_tokens": 1}))
                self.waiting.set()
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                self.closes += 1

        body = WaitingBody()
        upstream.return_value = httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 123),
            "server": ("testserver", 80),
        }
        supplied = False
        sent = []

        async def receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {
                    "type": "http.request",
                    "body": json.dumps(request_body(stream=True)).encode(),
                    "more_body": False,
                }
            await body.waiting.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await asyncio.wait_for(priced_client.app(scope, receive, send), 2)
        headers = dict(
            next(message for message in sent if message["type"] == "http.response.start")["headers"]
        )
        calls = await priced_client.app.state.record_store.find_calls(
            request_id=headers[b"x-request-id"].decode()
        )
        assert len(calls) == 1
        call = calls[0]
        attempts = await priced_client.app.state.record_store.find_attempts(call["id"])
        assert len(attempts) == 1
        assert call["result"]["status"] == "cancelled"
        assert call["result"]["cancel_reason"] == "client_disconnected"
        assert attempts[0]["result"]["status"] == "cancelled"
        assert call["usage"]["status"] == "partial"
        assert call["usage"]["known_input_tokens"] == 12
        assert call["cost"]["amount"] is None
        assert body.closes == 1

    asyncio.run(run())


def test_missing_counter_is_null_and_agrees_with_record(
    priced_client: TestClient, upstream: Mock, completion: dict
) -> None:
    completion["usage"] = {"prompt_tokens": 5, "total_tokens": 999}
    upstream.return_value = httpx.Response(200, json=completion)

    response = priced_client.post("/v1/chat/completions", json=request_body())

    assert response.json()["usage"] == {
        "input_tokens": 5,
        "output_tokens": None,
        "total_tokens": None,
    }
    call, attempts = read_records(priced_client, response)
    assert call["usage"]["input_tokens"] == 5
    assert call["usage"]["output_tokens"] is None
    assert call["cost"]["amount"] is None
    assert Decimal(call["cost"]["known_amount"]) == Decimal("0.000005")
    assert attempts[0]["usage"]["status"] == "partial"


def test_early_stream_usage_is_not_a_final_output_total(
    priced_client: TestClient, upstream: Mock
) -> None:
    content = (
        sse(frame("first", usage={"prompt_tokens": 12, "completion_tokens": 1}))
        + sse(frame("last", finish="stop"))
        + sse("[DONE]")
    )
    upstream.return_value, body = response_stream(content)

    response = priced_client.post("/v1/chat/completions", json=request_body(stream=True))

    chunks = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert chunks[-1]["type"] == "completed"
    assert chunks[-1]["usage"] == {"input_tokens": 12, "output_tokens": None, "total_tokens": None}
    call, attempts = read_records(priced_client, response)
    assert call["result"]["status"] == "completed"
    assert call["usage"]["input_tokens"] == 12
    assert call["usage"]["output_tokens"] is None
    assert call["usage"]["known_output_tokens"] == 1
    assert call["cost"]["status"] == "partial"
    assert call["cost"]["amount"] is None
    assert Decimal(call["cost"]["known_amount"]) == Decimal("0.000014")
    assert attempts[0]["usage"]["known_output_tokens"] == 1
    assert body.closed


def test_regressing_usage_fails_without_overwriting_known_snapshot(
    priced_client: TestClient, upstream: Mock
) -> None:
    content = sse(frame("first", usage={"prompt_tokens": 12, "completion_tokens": 5}))
    content += sse(
        frame("last", finish="stop", usage={"prompt_tokens": 12, "completion_tokens": 2})
    ) + sse("[DONE]")
    upstream.return_value, body = response_stream(content)

    response = priced_client.post("/v1/chat/completions", json=request_body(stream=True))

    call, attempts = read_records(priced_client, response)
    assert call["result"]["status"] == "failed"
    assert call["error"]["code"] == "UPSTREAM_INVALID_RESPONSE"
    assert attempts[0]["usage"]["known_output_tokens"] == 5
    assert call["cost"]["amount"] is None
    assert body.closed


def test_public_usage_roundtrip_recomputes_total() -> None:
    usage = ChatCompletionUsage.model_validate(
        {"input_tokens": 4, "output_tokens": 5, "total_tokens": 999}
    )
    assert usage.model_dump() == {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}
    assert ChatCompletionUsage.model_validate(usage.model_dump()).total_tokens == 9


def test_local_query_command_reads_call_and_attempts(
    priced_client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from app.governance.__main__ import main

    response = priced_client.post("/v1/chat/completions", json=request_body())
    monkeypatch.setattr(
        "sys.argv", ["app.governance", "--request-id", response.headers["x-request-id"]]
    )
    main()
    calls = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert calls[0]["result"]["status"] == "completed"
    monkeypatch.setattr("sys.argv", ["app.governance", "--call-id", calls[0]["id"]])
    main()
    attempts = json.loads(capsys.readouterr().out)
    assert len(attempts) == 1
    assert attempts[0]["attempt_number"] == 1


def test_default_prices_leave_cost_unknown(chat_client: TestClient) -> None:
    response = chat_client.post("/v1/chat/completions", json=request_body())
    call, attempts = read_records(chat_client, response)
    assert call["usage"]["status"] == "complete"
    assert call["cost"]["status"] == "unpriced"
    assert call["cost"]["amount"] is None
    assert attempts[0]["cost"]["price_snapshot"] is None


def test_cancellation_before_dispatch_has_no_incurred_cost(
    priced_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.governance.context import CallContext, current_call
    from app.governance.invocation import invoke

    async def run() -> None:
        call = CallContext(
            priced_client.app.state.record_store,
            priced_client.app.state.price_book,
            priced_client.app.state.settings,
        )
        await call.start()
        request = LLMRequest.model_validate(request_body())
        call.capture_prompt(request, request)
        call.route(priced_client.app.state.model_registry.get_configuration(request.model))
        adapter = Mock()
        adapter.complete = AsyncMock()
        begin = call.begin_attempt
        token = current_call.set(call)
        try:
            with anyio.CancelScope() as scope:

                async def cancelled_begin(request, repair):
                    attempt = await begin(request, repair)
                    scope.cancel()
                    return attempt

                monkeypatch.setattr(call, "begin_attempt", cancelled_begin)
                try:
                    await invoke(adapter, request)
                except asyncio.CancelledError:
                    call.terminal("cancelled")
                    raise
            await call.finish(None)
        finally:
            current_call.reset(token)
        adapter.complete.assert_not_awaited()
        saved = (await call.store.find_calls(request_id=call.record.request_id))[0]
        attempts = await call.store.find_attempts(saved["id"])
        assert saved["result"]["status"] == "cancelled"
        assert saved["result"]["http_status"] is None
        assert saved["resilience"]["attempt_count"] == 0
        assert saved["cost"]["status"] == "not_incurred"
        assert Decimal(saved["cost"]["amount"]) == 0
        assert attempts[0]["result"]["upstream_dispatched"] is False
        assert attempts[0]["result"]["status"] == "cancelled"

    asyncio.run(run())
