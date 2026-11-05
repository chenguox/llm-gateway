"""Consistent error responses for HTTP, validation and unexpected errors."""

import logging
from json import JSONDecodeError
from typing import Any

import httpx2 as httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
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


def map_upstream_error(exc: Exception) -> GatewayError | None:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError(504, "UPSTREAM_TIMEOUT", "Model provider request timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            return GatewayError(429, "UPSTREAM_RATE_LIMITED", "Model provider rate limit exceeded")
        return GatewayError(502, "UPSTREAM_HTTP_ERROR", "Model provider request failed")
    if isinstance(exc, httpx.RequestError):
        return GatewayError(502, "UPSTREAM_CONNECTION_ERROR", "Model provider connection failed")
    if isinstance(exc, (JSONDecodeError, UnicodeDecodeError, ValidationError)):
        return GatewayError(
            502, "UPSTREAM_INVALID_RESPONSE", "Model provider returned an invalid response"
        )
    return None


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
    from app.governance.context import current_call

    call = current_call.get()
    if call is not None:
        call.error(exc.code)
    return error_response(exc.status_code, exc.code, exc.message, details=exc.details)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    from app.governance.context import current_call

    call = current_call.get()
    if call is not None:
        call.error("HTTP_ERROR")
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
    from app.governance.context import current_call

    call = current_call.get()
    if call is not None:
        call.error("VALIDATION_ERROR", stage="validation")
    # Exclude submitted values and validation context, which may contain secrets.
    details = [
        {"location": error["loc"], "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(422, "VALIDATION_ERROR", "Request validation failed", details=details)


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception method=%s category=%s",
        request.method,
        type(exc).__name__,
    )
    return error_response(500, "INTERNAL_ERROR", "Internal server error")


def register_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(GatewayError, gateway_exception_handler)
    application.add_exception_handler(HTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(Exception, unexpected_exception_handler)
