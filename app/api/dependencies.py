"""Resolve application-owned model adapters for API requests."""

from fastapi import Request

from app.adapters.base import ModelAdapter
from app.core.errors import GatewayError


def get_model_adapter(request: Request) -> ModelAdapter:
    adapter = request.app.state.model_adapter
    if adapter is None:
        raise GatewayError(503, "PROVIDER_NOT_CONFIGURED", "Model provider is not configured")
    return adapter
