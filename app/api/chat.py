"""Public, non-streaming chat completions endpoint."""

from json import JSONDecodeError
from typing import Annotated

import httpx2 as httpx
from fastapi import APIRouter, Depends
from pydantic import ValidationError

from app.api.dependencies import get_model_registry, get_prompt_registry
from app.core.errors import GatewayError
from app.models.registry import ModelRegistry
from app.prompts.registry import PromptRegistry
from app.schemas.chat import LLMRequest, LLMResponse

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post("/chat/completions", response_model=LLMResponse, response_model_exclude_unset=True)
async def chat_completions(
    request: LLMRequest,
    models: Annotated[ModelRegistry, Depends(get_model_registry)],
    prompts: Annotated[PromptRegistry, Depends(get_prompt_registry)],
) -> LLMResponse:
    request = prompts.build_request(request)
    try:
        return await models.complete(request)
    except httpx.TimeoutException as exc:
        raise GatewayError(504, "UPSTREAM_TIMEOUT", "Model provider request timed out") from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise GatewayError(
                429, "UPSTREAM_RATE_LIMITED", "Model provider rate limit exceeded"
            ) from exc
        raise GatewayError(502, "UPSTREAM_HTTP_ERROR", "Model provider request failed") from exc
    except httpx.RequestError as exc:
        raise GatewayError(
            502, "UPSTREAM_CONNECTION_ERROR", "Model provider connection failed"
        ) from exc
    except (JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise GatewayError(
            502, "UPSTREAM_INVALID_RESPONSE", "Model provider returned an invalid response"
        ) from exc
