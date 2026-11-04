"""Application entry point: uvicorn app.main:app."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.models.registry import ModelRegistry
from app.prompts.registry import PromptRegistry


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with validated configuration."""
    settings = settings if settings is not None else get_settings()
    configure_logging(settings.log_level)
    models = ModelRegistry.from_file(settings.models_config_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with models.connect(settings):
            yield

    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.model_registry = models
    application.state.prompt_registry = PromptRegistry()
    register_exception_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
