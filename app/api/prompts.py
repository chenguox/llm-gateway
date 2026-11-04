"""Discover available system prompt names, versions and parameters."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_prompt_registry
from app.prompts.registry import PromptRegistry
from app.schemas.prompts import PromptTemplateList

router = APIRouter(prefix="/v1", tags=["prompts"])


@router.get("/prompts", response_model=PromptTemplateList)
def list_prompts(
    prompts: Annotated[PromptRegistry, Depends(get_prompt_registry)],
) -> PromptTemplateList:
    return PromptTemplateList(data=prompts.list_templates())
