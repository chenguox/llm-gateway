"""Application entry point: uvicorn app.main:app."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.governance.middleware import CallRecordingMiddleware
from app.governance.pricing import PriceBook
from app.governance.store import RecordStorageError, RecordStore
from app.models.registry import ModelRegistry
from app.prompts.registry import PromptRegistry


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with validated configuration."""
    settings = settings if settings is not None else get_settings()
    configure_logging(settings.log_level)
    models = ModelRegistry.from_file(settings.models_config_path)
    prices = PriceBook.from_file(settings.prices_config_path)
    records = RecordStore(settings.call_records_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            await records.initialize(settings.call_records_retention_days)
        except RecordStorageError:
            logging.getLogger(__name__).error("CALL_RECORD_STORAGE_UNAVAILABLE stage=startup")
        async with anyio.create_task_group() as maintenance:
            maintenance.start_soon(records.maintain, settings.call_records_retention_days)
            try:
                async with models.connect(settings):
                    yield
            finally:
                maintenance.cancel_scope.cancel()

    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.model_registry = models
    application.state.prompt_registry = PromptRegistry()
    application.state.record_store = records
    application.state.price_book = prices
    application.add_middleware(
        CallRecordingMiddleware, store=records, prices=prices, settings=settings
    )
    register_exception_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
