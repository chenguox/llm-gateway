"""Load, resolve and render file-backed system prompt templates."""

import json
from pathlib import Path
from string import Template

from app.core.errors import GatewayError
from app.schemas.chat import ChatMessage, LLMRequest
from app.schemas.prompts import PromptSelection, PromptTemplate

TEMPLATES_DIRECTORY = Path(__file__).parent / "templates"


class PromptRegistry:
    def __init__(self, directory: Path = TEMPLATES_DIRECTORY) -> None:
        self._templates: dict[tuple[str, str], PromptTemplate] = {}
        if not directory.is_dir():
            raise ValueError(f"System prompt templates directory does not exist: {directory}")
        for path in sorted(directory.rglob("*.json")):
            try:
                template = PromptTemplate.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ValueError(f"Invalid system prompt template: {path}") from exc
            content = Template(template.content)
            if not content.is_valid():
                raise ValueError(f"Invalid system prompt placeholders: {path}")
            if set(content.get_identifiers()) != set(template.parameters):
                raise ValueError(f"System prompt placeholders do not match parameters: {path}")
            key = (template.name, template.version)
            if key in self._templates:
                raise ValueError(
                    f"Duplicate system prompt template: {template.name}@{template.version}"
                )
            self._templates[key] = template

    def list_templates(self) -> list[PromptTemplate]:
        return [self._templates[key] for key in sorted(self._templates)]

    def render(self, selection: PromptSelection) -> str:
        template = self._templates.get((selection.name, selection.version))
        if template is None:
            raise GatewayError(
                404, "PROMPT_TEMPLATE_NOT_FOUND", "System prompt template or version was not found"
            )
        unknown = set(selection.parameters) - set(template.parameters)
        missing = {
            name
            for name, parameter in template.parameters.items()
            if parameter.required and name not in selection.parameters
        }
        if missing or unknown:
            reasons = []
            if missing:
                reasons.append(f"Missing parameters: {', '.join(sorted(missing))}")
            if unknown:
                reasons.append(f"Unknown parameters: {', '.join(sorted(unknown))}")
            raise GatewayError(422, "PROMPT_PARAMETER_ERROR", "; ".join(reasons))
        values = {}
        for name, parameter in template.parameters.items():
            value = selection.parameters.get(name, parameter.default)
            values[name] = (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            )
        return Template(template.content).substitute(values)

    def build_request(self, request: LLMRequest) -> LLMRequest:
        if request.system_prompt is None:
            return request
        content = self.render(request.system_prompt)
        messages = [
            ChatMessage(role="system", content=content),
            *(message for message in request.messages if message.role != "system"),
        ]
        return request.model_copy(update={"messages": messages, "system_prompt": None})
