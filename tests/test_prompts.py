"""Versioned prompt loading, rendering and public catalog contracts."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.errors import GatewayError
from app.prompts.registry import PromptRegistry
from app.schemas.chat import LLMRequest
from app.schemas.prompts import PromptSelection


@pytest.fixture
def template() -> dict:
    return {
        "name": "demo",
        "version": "1.0.0",
        "description": "Example prompt",
        "content": "You are ${role}. Use ${language}. Context: ${context}.",
        "parameters": {
            "role": {"description": "Assistant role", "required": True},
            "language": {"required": False, "default": "English"},
            "context": {"required": False, "default": {}},
        },
    }


@pytest.fixture
def registry(tmp_path: Path, template: dict) -> PromptRegistry:
    (tmp_path / "prompt.json").write_text(json.dumps(template), encoding="utf-8")
    return PromptRegistry(tmp_path)


def test_render_uses_supplied_parameters_and_defaults(registry: PromptRegistry) -> None:
    result = registry.render(
        PromptSelection(name="demo", version="1.0.0", parameters={"role": "teacher"})
    )

    assert result == "You are teacher. Use English. Context: {}."


@pytest.mark.parametrize("context", [23, 1.5, False, None, ["one", "two"], {"topic": "中文"}])
def test_render_serializes_json_parameter_values(registry: PromptRegistry, context) -> None:
    result = registry.render(
        PromptSelection(
            name="demo", version="1.0.0", parameters={"role": "teacher", "context": context}
        )
    )

    assert (
        result
        == f"You are teacher. Use English. Context: {json.dumps(context, ensure_ascii=False)}."
    )


def test_parameter_values_are_not_interpreted_as_templates(registry: PromptRegistry) -> None:
    result = registry.render(
        PromptSelection(
            name="demo",
            version="1.0.0",
            parameters={"role": "${language} $dollar {object.attribute}"},
        )
    )

    assert result.startswith("You are ${language} $dollar {object.attribute}.")


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "Missing parameters: role"),
        ({"role": "teacher", "unknown": "private-value"}, "Unknown parameters: unknown"),
    ],
)
def test_parameter_errors_hide_values(
    registry: PromptRegistry, parameters: dict, message: str
) -> None:
    with pytest.raises(GatewayError) as error:
        registry.render(PromptSelection(name="demo", version="1.0.0", parameters=parameters))

    assert error.value.status_code == 422
    assert error.value.code == "PROMPT_PARAMETER_ERROR"
    assert error.value.message == message
    assert "private-value" not in error.value.message


@pytest.mark.parametrize(
    ("name", "version"),
    [("missing", "1.0.0"), ("demo", "latest"), ("demo", "2.0.0"), ("../../demo", "1.0.0")],
)
def test_template_lookup_requires_exact_name_and_version(
    registry: PromptRegistry, name: str, version: str
) -> None:
    with pytest.raises(GatewayError) as error:
        registry.render(PromptSelection(name=name, version=version, parameters={"role": "teacher"}))

    assert error.value.status_code == 404
    assert error.value.code == "PROMPT_TEMPLATE_NOT_FOUND"


def test_build_request_replaces_system_messages_without_mutating_input(
    registry: PromptRegistry,
) -> None:
    request = LLMRequest(
        model="demo-model",
        system_prompt=PromptSelection(name="demo", version="1.0.0", parameters={"role": "teacher"}),
        messages=[
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "another old system"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Continue"},
        ],
        max_tokens=64,
    )

    result = registry.build_request(request)

    assert result.messages[0].content == "You are teacher. Use English. Context: {}."
    assert [message.role for message in result.messages] == ["system", "user", "assistant", "user"]
    assert [message.content for message in result.messages[1:]] == ["Hello", "Hi", "Continue"]
    assert result.system_prompt is None
    assert "system_prompt" not in result.model_dump()
    assert result.max_tokens == 64
    assert request.messages[0].content == "old system"
    assert len(request.messages) == 5
    assert request.system_prompt is not None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"content": "${role"}, "Invalid system prompt placeholders"),
        ({"content": "${undeclared}"}, "placeholders do not match parameters"),
        ({"content": ""}, "Invalid system prompt template"),
        ({"parameters": []}, "Invalid system prompt template"),
    ],
)
def test_invalid_templates_fail_during_loading(
    tmp_path: Path, template: dict, overrides: dict, message: str
) -> None:
    (tmp_path / "invalid.json").write_text(json.dumps({**template, **overrides}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        PromptRegistry(tmp_path)


def test_invalid_json_fails_during_loading(tmp_path: Path) -> None:
    (tmp_path / "invalid.json").write_text("not JSON", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid system prompt template"):
        PromptRegistry(tmp_path)


def test_duplicate_template_versions_are_rejected(tmp_path: Path, template: dict) -> None:
    for filename in ["first.json", "second.json"]:
        (tmp_path / filename).write_text(json.dumps(template), encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate system prompt template: demo@1.0.0"):
        PromptRegistry(tmp_path)


def test_default_templates_do_not_depend_on_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    templates = PromptRegistry().list_templates()

    assert ("assistant", "1.0.0") in {(template.name, template.version) for template in templates}


def test_prompt_catalog_lists_versions_and_parameter_requirements(client: TestClient) -> None:
    response = client.get("/v1/prompts")

    assert response.status_code == 200
    templates = response.json()["data"]
    identities = [(template["name"], template["version"]) for template in templates]
    assert identities == sorted(identities)
    assert ("assistant", "1.0.0") in identities
    assert ("assistant", "2.0.0") in identities
    customer_support = next(
        template for template in templates if template["name"] == "customer_support"
    )
    assert customer_support["parameters"]["product"]["required"] is True
    assert customer_support["parameters"]["language"]["default"] == "中文"
    assert all("content" not in template for template in templates)


def test_openapi_exposes_prompt_selection_and_catalog(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/v1/prompts" in schema["paths"]
    assert "system_prompt" in schema["components"]["schemas"]["LLMRequest"]["properties"]
    assert "PromptSelection" in schema["components"]["schemas"]
