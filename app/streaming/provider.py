"""Own an upstream HTTP response and decode SSE records incrementally."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import httpx2 as httpx

from app.core.errors import GatewayError
from app.governance.context import capture_headers
from app.schemas.streaming import StreamUpdate

if TYPE_CHECKING:
    from app.governance.context import AttemptContext


def invalid_stream() -> GatewayError:
    return GatewayError(
        502, "UPSTREAM_INVALID_RESPONSE", "Model provider returned an invalid stream"
    )


@dataclass(frozen=True)
class SSERecord:
    event: str
    data: str


async def read_sse(response: httpx.Response) -> AsyncGenerator[SSERecord]:
    data: list[str] = []
    event = "message"
    size = 0
    first_line = True
    async for line in response.aiter_lines():
        if first_line:
            line = line.removeprefix("\ufeff")
            first_line = False
        if not line:
            if data:
                yield SSERecord(event, "\n".join(data))
            data = []
            event = "message"
            size = 0
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            size += len(value)
            if size > 1_048_576:
                raise invalid_stream()
            data.append(value)
        elif field == "event":
            event = value
    if data:
        raise invalid_stream()


class CompletionStream:
    def __init__(self, response: httpx.Response, updates: AsyncGenerator[StreamUpdate]) -> None:
        self.response = response
        self._updates = updates
        self._closed = False
        self.observer: AttemptContext | None = None
        self._observed_iterator: AsyncGenerator[StreamUpdate] | None = None

    def __aiter__(self) -> AsyncGenerator[StreamUpdate]:
        if self.observer is None:
            return self._updates
        if self._observed_iterator is None:
            self._observed_iterator = self._observe()
        return self._observed_iterator

    async def _observe(self) -> AsyncGenerator[StreamUpdate]:
        observer = self.observer
        try:
            async for update in self._updates:
                observer.observe(update.usage)
                if update.delta and (update.delta.content or update.delta.reasoning_content):
                    observer.first_content()
                if update.type == "completed":
                    await observer.finish(
                        "completed",
                        reasons=[reason.finish_reason for reason in update.finish_reasons or []],
                    )
                yield update
        except (asyncio.CancelledError, GeneratorExit):
            with anyio.CancelScope(shield=True):
                await observer.finish("cancelled")
            raise
        except Exception as exc:
            await observer.finish("failed", exc=exc)
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            try:
                if self._observed_iterator is not None:
                    await self._observed_iterator.aclose()
            finally:
                try:
                    await self._updates.aclose()
                finally:
                    try:
                        if self.observer is not None:
                            await self.observer.finish("cancelled")
                    finally:
                        await self.response.aclose()


async def open_completion_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    timeout: float,
    normalize: Callable[[httpx.Response], AsyncGenerator[StreamUpdate]],
) -> CompletionStream:
    request = client.build_request(
        "POST",
        url,
        headers={**headers, "Accept": "text/event-stream"},
        json=payload,
        timeout=timeout,
    )
    response = await client.send(request, stream=True)
    try:
        capture_headers(response.headers, response.status_code)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "text/event-stream":
            raise invalid_stream()
        return CompletionStream(response, normalize(response))
    except BaseException:
        with anyio.CancelScope(shield=True):
            await response.aclose()
        raise
