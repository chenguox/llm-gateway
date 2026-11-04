"""Application entry point: uvicorn app.main:app."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.deepseek import DeepSeekChatAdapter
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.prompts.registry import PromptRegistry


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with validated configuration."""
    settings = settings if settings is not None else get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        api_key = settings.deepseek_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            yield
            return
        async with DeepSeekChatAdapter.from_settings(settings) as adapter:
            application.state.model_adapter = adapter
            try:
                yield
            finally:
                application.state.model_adapter = None

    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.model_adapter = None
    application.state.prompt_registry = PromptRegistry()
    register_exception_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
