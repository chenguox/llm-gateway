"""Application, model configuration and mocked upstream fixtures."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def isolated_call_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_RECORDS_PATH", str(tmp_path / "calls.sqlite3"))
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)


@pytest.fixture
def model_configuration() -> dict:
    return {
        "models": [
            {
                "logical_name": "general-chat",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "base_url": "https://provider.test/v1",
                "api_key_env": "TEST_GENERAL_CHAT_API_KEY",
                "timeout_seconds": 5,
                "capabilities": {
                    "chat": True,
                    "structured_output": {
                        "supported": True,
                        "modes": ["json_object", "json_schema"],
                    },
                },
            },
            {
                "logical_name": "coding-assistant",
                "provider": "openai_compatible",
                "model": "provider-coder",
                "base_url": "https://coder.test/v2",
                "api_key_env": "TEST_CODING_ASSISTANT_API_KEY",
                "timeout_seconds": 7,
                "capabilities": {"chat": True, "max_output_tokens": 256},
            },
        ]
    }


@pytest.fixture
def model_config_path(
    model_configuration: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    for model in model_configuration["models"]:
        monkeypatch.delenv(model["api_key_env"], raising=False)
    path = tmp_path / "models.json"
    path.write_text(json.dumps(model_configuration), encoding="utf-8")
    return path


@pytest.fixture
def application(model_config_path: Path) -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            log_level="WARNING",
            models_config_path=model_config_path,
        )
    )


@pytest.fixture
def client(application: FastAPI) -> Iterator[TestClient]:
    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client


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

    def create_client(**kwargs: object) -> httpx.AsyncClient:
        client = client_class(transport=httpx.MockTransport(handler))
        upstream_clients.append(client)
        return client

    monkeypatch.setattr("app.adapters.compatible.httpx.AsyncClient", create_client)
    return handler


@pytest.fixture
def chat_application(
    upstream: Mock, model_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    monkeypatch.setenv("TEST_GENERAL_CHAT_API_KEY", "unit-test-key")
    monkeypatch.setenv("TEST_CODING_ASSISTANT_API_KEY", "coding-test-key")
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            log_level="WARNING",
            models_config_path=model_config_path,
        )
    )


@pytest.fixture
def chat_client(chat_application: FastAPI) -> Iterator[TestClient]:
    with TestClient(chat_application, raise_server_exceptions=False) as client:
        yield client
