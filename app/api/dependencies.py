"""Resolve application-owned model adapters for API requests."""

from fastapi import Request

from app.models.registry import ModelRegistry
from app.prompts.registry import PromptRegistry


def get_prompt_registry(request: Request) -> PromptRegistry:
    return request.app.state.prompt_registry


def get_model_registry(request: Request) -> ModelRegistry:
    return request.app.state.model_registry
