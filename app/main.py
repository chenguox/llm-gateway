"""Application entry point: uvicorn app.main:app."""

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application with validated configuration."""
    settings = settings if settings is not None else get_settings()
    configure_logging(settings.log_level)

    application = FastAPI(title=settings.app_name, version="0.1.0")
    application.state.settings = settings
    register_exception_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
