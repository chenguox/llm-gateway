"""Record every chat request, including rejected requests and streaming lifetimes."""

import asyncio
import logging

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.errors import GatewayError, error_response, map_upstream_error
from app.governance.context import CallContext, current_call
from app.governance.pricing import PriceBook
from app.governance.store import RecordStore

logger = logging.getLogger(__name__)


class CallRecordingMiddleware:
    def __init__(
        self, app: ASGIApp, *, store: RecordStore, prices: PriceBook, settings: Settings
    ) -> None:
        self.app, self.store, self.prices, self.settings = app, store, prices, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] != "/v1/chat/completions"
            or scope["method"] != "POST"
        ):
            await self.app(scope, receive, send)
            return
        call = CallContext(self.store, self.prices, self.settings)
        token = current_call.set(call)
        status = None
        started = False
        created = False

        async def send_recorded(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                status = message["status"]
                started = True
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                message = {
                    **message,
                    "headers": [*headers, (b"x-request-id", call.record.request_id.encode())],
                }
            await send(message)

        async def receive_recorded() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect":
                call.terminal("cancelled", cancel_reason="client_disconnected")
            return message

        try:
            with anyio.CancelScope(shield=True):
                await call.start()
                created = True
            await self.app(scope, receive_recorded, send_recorded)
        except asyncio.CancelledError:
            call.terminal("cancelled")
            raise
        except Exception as exc:
            error = map_upstream_error(exc) or GatewayError(
                500, "INTERNAL_ERROR", "Internal server error"
            )
            call.error(error.code, category=type(exc).__name__)
            call.terminal("failed")
            if not started:
                await error_response(error.status_code, error.code, error.message)(
                    scope, receive, send_recorded
                )
            else:
                logger.error(
                    "CALL_EXECUTION_FAILED request_id=%s code=%s",
                    call.record.request_id,
                    error.code,
                )
        finally:
            try:
                if created:
                    with anyio.CancelScope(shield=True):
                        await call.finish(status)
            finally:
                current_call.reset(token)
