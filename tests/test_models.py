"""Logical model configuration, credential isolation and client routing."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.base import ModelAdapter
from app.adapters.compatible import ChatCompletionsAdapter
from app.adapters.deepseek import DeepSeekChatAdapter
from app.core.config import Settings
from app.core.errors import GatewayError
from app.main import create_app
from app.models.registry import ModelRegistry
from app.schemas.models import ModelsConfiguration


@pytest.mark.parametrize(
    "overrides",
    [
        {"logical_name": ""},
        {"logical_name": "   "},
        {"model": ""},
        {"provider": "unknown"},
        {"base_url": "not-a-url"},
        {"base_url": "ftp://provider.test"},
        {"api_key_env": ""},
        {"api_key_env": "1_INVALID"},
        {"api_key_env": "KEY-NAME"},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("inf")},
        {"capabilities": {"chat": "false"}},
        {"capabilities": {"max_output_tokens": 0}},
        {"capabilities": {"max_output_tokens": 1.5}},
        {"capabilities": {"context_window": -1}},
        {"capabilities": {"context_window": 256, "max_output_tokens": 512}},
        {"capabilities": {"structured_output": {"supported": True, "modes": []}}},
        {"capabilities": {"structured_output": {"supported": False, "modes": ["json_object"]}}},
        {"capabilities": {"structured_output": {"supported": True, "modes": ["unsupported"]}}},
        {
            "capabilities": {
                "structured_output": {"supported": True, "modes": ["json_schema", "json_schema"]}
            }
        },
        {"api_key": "must-not-be-stored-in-config"},
    ],
)
def test_invalid_model_configuration_is_rejected(
    model_configuration: dict, overrides: dict
) -> None:
    model_configuration["models"][0].update(overrides)

    with pytest.raises(ValidationError):
        ModelsConfiguration.model_validate(model_configuration)


@pytest.mark.parametrize("field", ["logical_name", "api_key_env"])
def test_models_cannot_share_logical_names_or_key_variables(
    model_configuration: dict, field: str
) -> None:
    model_configuration["models"][1][field] = model_configuration["models"][0][field]

    with pytest.raises(ValidationError):
        ModelsConfiguration.model_validate(model_configuration)


def test_key_variable_uniqueness_is_case_insensitive(model_configuration: dict) -> None:
    model_configuration["models"][1]["api_key_env"] = model_configuration["models"][0][
        "api_key_env"
    ].lower()

    with pytest.raises(ValidationError, match="different API key environment variable"):
        ModelsConfiguration.model_validate(model_configuration)


@pytest.mark.parametrize(
    "modes", [["json_object"], ["json_schema"], ["json_object", "json_schema"]]
)
def test_structured_output_modes_can_be_configured(
    model_configuration: dict, modes: list[str]
) -> None:
    model_configuration["models"][0]["capabilities"]["structured_output"] = {
        "supported": True,
        "modes": modes,
    }

    configuration = ModelsConfiguration.model_validate(model_configuration)

    assert configuration.models[0].capabilities.structured_output.modes == modes


@pytest.mark.parametrize("content", ["not JSON", "{}", '{"models":[{}]}'])
def test_invalid_config_file_prevents_application_creation(tmp_path: Path, content: str) -> None:
    path = tmp_path / "models.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid model configuration file"):
        create_app(Settings(_env_file=None, models_config_path=path))


def test_missing_config_file_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid model configuration file"):
        ModelRegistry.from_file(tmp_path / "missing.json")


def test_default_model_config_path_is_independent_of_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODELS_CONFIG_PATH", raising=False)

    registry = ModelRegistry.from_file(Settings(_env_file=None).models_config_path)

    assert {model.id for model in registry.list_models()} == {
        "general-chat",
        "coding-assistant",
        "flash-chat",
    }


def test_model_catalog_exposes_only_logical_names_and_capabilities(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [model["id"] for model in body["data"]] == ["coding-assistant", "general-chat"]
    assert all(set(model) == {"id", "object", "capabilities"} for model in body["data"])
    general = next(model for model in body["data"] if model["id"] == "general-chat")
    assert general["capabilities"]["structured_output"] == {
        "supported": True,
        "modes": ["json_object", "json_schema"],
    }
    for private_value in [
        "deepseek-chat",
        "provider-coder",
        "provider.test",
        "coder.test",
        "TEST_GENERAL_CHAT_API_KEY",
        "TEST_CODING_ASSISTANT_API_KEY",
        "api_key_env",
        "base_url",
        "provider",
    ]:
        assert private_value not in response.text


def test_logical_models_select_independent_clients_and_hide_provider_names(
    chat_client: TestClient, upstream: Mock, completion: dict
) -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json={**completion, "model": payload["model"]})

    upstream.side_effect = handle_request
    for logical_name in ["general-chat", "coding-assistant"]:
        response = chat_client.post(
            "/v1/chat/completions",
            json={"model": logical_name, "messages": [{"role": "user", "content": "Hello"}]},
        )
        assert response.status_code == 200
        assert response.json()["model"] == logical_name
        assert "deepseek-chat" not in response.text
        assert "provider-coder" not in response.text
    general_request, coding_request = [call.args[0] for call in upstream.call_args_list]
    assert str(general_request.url) == "https://provider.test/v1/chat/completions"
    assert general_request.headers["authorization"] == "Bearer unit-test-key"
    assert json.loads(general_request.content)["model"] == "deepseek-chat"
    assert general_request.extensions["timeout"]["read"] == 5
    assert str(coding_request.url) == "https://coder.test/v2/chat/completions"
    assert coding_request.headers["authorization"] == "Bearer coding-test-key"
    assert json.loads(coding_request.content)["model"] == "provider-coder"
    assert json.loads(coding_request.content)["max_tokens"] == 256
    assert coding_request.extensions["timeout"]["read"] == 7
    models = chat_client.app.state.model_registry
    assert isinstance(models.get_adapter("general-chat"), DeepSeekChatAdapter)
    assert isinstance(models.get_adapter("coding-assistant"), ChatCompletionsAdapter)
    assert models.get_adapter("general-chat") is not models.get_adapter("coding-assistant")


@pytest.mark.parametrize("name", ["unknown", "deepseek-chat", "provider-coder", "../general-chat"])
def test_unknown_logical_models_never_fall_back(
    chat_client: TestClient, upstream: Mock, name: str
) -> None:
    response = chat_client.post(
        "/v1/chat/completions",
        json={"model": name, "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"
    upstream.assert_not_called()


def test_one_missing_key_does_not_affect_other_models_or_use_legacy_key(
    application: FastAPI, upstream: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_GENERAL_CHAT_API_KEY", "unit-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key-must-not-be-used")
    with TestClient(application) as client:
        available = client.post(
            "/v1/chat/completions",
            json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
        )
        unavailable = client.post(
            "/v1/chat/completions",
            json={"model": "coding-assistant", "messages": [{"role": "user", "content": "Hello"}]},
        )
        assert available.status_code == 200
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"
        assert "TEST_CODING_ASSISTANT_API_KEY" not in unavailable.text
        assert client.get("/health").status_code == 200
        assert client.get("/v1/models").status_code == 200
    upstream.assert_called_once()


def test_chat_capability_is_checked_before_calling_provider(
    model_configuration: dict, tmp_path: Path, upstream: Mock
) -> None:
    model_configuration["models"][0]["capabilities"]["chat"] = False
    path = tmp_path / "disabled-chat.json"
    path.write_text(json.dumps(model_configuration), encoding="utf-8")
    application = create_app(Settings(_env_file=None, models_config_path=path))
    with TestClient(application) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "general-chat", "messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CAPABILITY_NOT_SUPPORTED"
    upstream.assert_not_called()


@pytest.mark.parametrize("max_tokens", [128, 256])
def test_valid_output_token_limits_are_forwarded(
    chat_client: TestClient, upstream: Mock, max_tokens: int
) -> None:
    response = chat_client.post(
        "/v1/chat/completions",
        json={
            "model": "coding-assistant",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": max_tokens,
        },
    )

    assert response.status_code == 200
    assert json.loads(upstream.call_args.args[0].content)["max_tokens"] == max_tokens


def test_excessive_output_tokens_are_rejected(chat_client: TestClient, upstream: Mock) -> None:
    response = chat_client.post(
        "/v1/chat/completions",
        json={
            "model": "coding-assistant",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 257,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_TOKEN_LIMIT_EXCEEDED"
    upstream.assert_not_called()


def test_capability_metadata_does_not_enable_response_format(
    chat_client: TestClient, upstream: Mock
) -> None:
    response = chat_client.post(
        "/v1/chat/completions",
        json={
            "model": "general-chat",
            "messages": [{"role": "user", "content": "Hello"}],
            "response_format": {"type": "json_object"},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    upstream.assert_not_called()


def test_partial_client_initialization_failure_closes_created_clients(
    model_configuration: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ModelRegistry(ModelsConfiguration.model_validate(model_configuration))
    for configuration in model_configuration["models"]:
        monkeypatch.setenv(configuration["api_key_env"], "test-key")
    adapter = AsyncMock(spec=ModelAdapter)
    factory = Mock(side_effect=[adapter, RuntimeError("Client creation failed")])
    monkeypatch.setattr("app.models.registry.create_adapter", factory)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="Client creation failed"):
            async with registry.connect(Settings(_env_file=None)):
                pytest.fail("Application must not finish startup after a client creation error")

    asyncio.run(run())

    adapter.aclose.assert_awaited_once()
    with pytest.raises(GatewayError, match="Model credentials are not configured"):
        registry.get_adapter("general-chat")


def test_openapi_exposes_logical_model_catalog(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/v1/models" in schema["paths"]
    assert "StructuredOutputCapabilities" in schema["components"]["schemas"]
    assert "ModelConfiguration" not in schema["components"]["schemas"]


def test_model_clients_load_separate_keys_from_dotenv(
    model_config_path: Path, tmp_path: Path, upstream: Mock
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TEST_GENERAL_CHAT_API_KEY=dotenv-general\nTEST_CODING_ASSISTANT_API_KEY=dotenv-coding\n",
        encoding="utf-8",
    )
    application = create_app(Settings(_env_file=env_file, models_config_path=model_config_path))

    with TestClient(application) as client:
        for name in ["general-chat", "coding-assistant"]:
            response = client.post(
                "/v1/chat/completions",
                json={"model": name, "messages": [{"role": "user", "content": "Hello"}]},
            )
            assert response.status_code == 200
            assert response.json()["model"] == name

    assert [call.args[0].headers["authorization"] for call in upstream.call_args_list] == [
        "Bearer dotenv-general",
        "Bearer dotenv-coding",
    ]
