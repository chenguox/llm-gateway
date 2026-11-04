"""Assign stable event identities and arbitrate a stream's single terminal state."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from uuid import uuid4

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.errors import GatewayError, map_upstream_error
from app.schemas.chat import ChatCompletionUsage
from app.schemas.streaming import StreamError, StreamEvent
from app.streaming.provider import CompletionStream, invalid_stream

logger = logging.getLogger(__name__)
_TERMINAL = {"completed", "failed", "cancelled"}


class StreamSession:
    def __init__(self, upstream: CompletionStream, model: str) -> None:
        self.upstream = upstream
        self.model = model
        self.stream_id = f"stream-{uuid4().hex}"
        self.sequence = 0
        self.terminal: StreamEvent | None = None
        self.usage: ChatCompletionUsage | None = None
        self._closed = False

    def _event(self, kind: str, **payload: object) -> StreamEvent | None:
        if self.terminal is not None:
            return None
        self.sequence += 1
        event = StreamEvent(
            type=kind,
            stream_id=self.stream_id,
            sequence=self.sequence,
            model=self.model,
            **payload,
        )
        if kind in _TERMINAL:
            self.terminal = event
            logger.info(
                "Stream %s model=%s terminal=%s code=%s",
                self.stream_id,
                self.model,
                kind,
                event.error.code if event.error else None,
            )
        return event

    def cancel(self, reason: str = "client_disconnected") -> StreamEvent | None:
        return self._event("cancelled", reason=reason, usage=self.usage)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel()
        with anyio.CancelScope(shield=True):
            try:
                await self.upstream.aclose()
            except Exception:
                logger.exception("Failed to close upstream for stream %s", self.stream_id)

    async def events(self) -> AsyncGenerator[StreamEvent]:
        try:
            event = self._event("started")
            if event is None:
                return
            yield event
            async for update in self.upstream:
                if self.terminal is not None:
                    return
                if update.usage is not None:
                    self.usage = update.usage
                payload = update.model_dump(exclude={"type"}, exclude_none=True)
                if update.type == "completed" and self.usage is not None:
                    payload["usage"] = self.usage
                event = self._event(update.type, **payload)
                if event is not None:
                    yield event
                if self.terminal is not None:
                    return
            raise invalid_stream()
        except (asyncio.CancelledError, GeneratorExit):
            self.cancel("request_cancelled")
            raise
        except Exception as exc:
            error = map_upstream_error(exc)
            if error is None:
                logger.exception("Unexpected stream failure for %s", self.stream_id)
                error = GatewayError(500, "INTERNAL_ERROR", "Internal server error")
            event = self._event(
                "failed",
                error=StreamError(code=error.code, message=error.message),
                usage=self.usage,
            )
            if event is not None:
                yield event
        finally:
            await self.aclose()

    async def encode(self) -> AsyncGenerator[str]:
        events = self.events()
        try:
            async for event in events:
                yield (
                    f"id: {event.stream_id}:{event.sequence}\n"
                    f"event: {event.type}\n"
                    f"data: {event.model_dump_json(exclude_none=True)}\n\n"
                )
        finally:
            await events.aclose()


class GatewayStreamingResponse(StreamingResponse):
    def __init__(self, session: StreamSession) -> None:
        self.session = session
        super().__init__(
            session.encode(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with anyio.create_task_group() as tasks:

                async def send_stream() -> None:
                    try:
                        await self.stream_response(send)
                    except OSError:
                        self.session.cancel()
                    finally:
                        tasks.cancel_scope.cancel()

                tasks.start_soon(send_stream)
                await self.listen_for_disconnect(receive)
                self.session.cancel()
                tasks.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.session.aclose()
