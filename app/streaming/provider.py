"""Own an upstream HTTP response and decode SSE records incrementally."""

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

import anyio
import httpx2 as httpx

from app.core.errors import GatewayError
from app.schemas.streaming import StreamUpdate


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

    def __aiter__(self) -> AsyncGenerator[StreamUpdate]:
        return self._updates

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            try:
                await self._updates.aclose()
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
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "text/event-stream":
            raise invalid_stream()
        return CompletionStream(response, normalize(response))
    except BaseException:
        with anyio.CancelScope(shield=True):
            await response.aclose()
        raise
