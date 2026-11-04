"""Unified chat completions endpoint for JSON responses and SSE."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_model_registry, get_prompt_registry
from app.core.errors import map_upstream_error
from app.models.registry import ModelRegistry
from app.prompts.registry import PromptRegistry
from app.schemas.chat import LLMRequest, LLMResponse
from app.streaming.provider import CompletionStream
from app.streaming.service import GatewayStreamingResponse, StreamSession

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post(
    "/chat/completions",
    response_model=LLMResponse,
    response_model_exclude_unset=True,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def chat_completions(
    request: LLMRequest,
    models: Annotated[ModelRegistry, Depends(get_model_registry)],
    prompts: Annotated[PromptRegistry, Depends(get_prompt_registry)],
) -> LLMResponse | GatewayStreamingResponse:
    request = prompts.build_request(request)
    try:
        response = await models.complete(request)
        if isinstance(response, CompletionStream):
            return GatewayStreamingResponse(StreamSession(response, request.model))
        return response
    except Exception as exc:
        error = map_upstream_error(exc)
        if error is None or error is exc:
            raise
        raise error from exc
