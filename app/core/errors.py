"""Consistent error responses for HTTP, validation and unexpected errors."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """An expected gateway failure with a public, sanitized error message."""

    def __init__(self, status_code: int, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


async def gateway_exception_handler(request: Request, exc: GatewayError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, details=exc.details)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return error_response(
        exc.status_code,
        "HTTP_ERROR",
        exc.detail if isinstance(exc.detail, str) else "Request failed",
        details=None if isinstance(exc.detail, str) else exc.detail,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Exclude submitted values and validation context, which may contain secrets.
    details = [
        {"location": error["loc"], "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(422, "VALIDATION_ERROR", "Request validation failed", details=details)


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception for %s %s",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return error_response(500, "INTERNAL_ERROR", "Internal server error")


def register_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(GatewayError, gateway_exception_handler)
    application.add_exception_handler(HTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(Exception, unexpected_exception_handler)
