"""Instrument the existing single adapter interface for each actual attempt."""

import asyncio

import anyio

from app.adapters.base import ModelAdapter
from app.governance.context import current_call
from app.schemas.chat import LLMRequest, LLMResponse
from app.streaming.provider import CompletionStream


async def invoke(
    adapter: ModelAdapter, request: LLMRequest, *, repair: bool = False
) -> LLMResponse | CompletionStream:
    call = current_call.get()
    if call is None:
        return await adapter.complete(request)
    call.stage = "recording"
    with anyio.CancelScope(shield=True):
        attempt = await call.begin_attempt(request, repair)
    call.stage = "upstream"
    try:
        await anyio.lowlevel.checkpoint()
        attempt.record.result.upstream_dispatched = True
        response = await adapter.complete(request)
        if isinstance(response, CompletionStream):
            response.observer = attempt
        else:
            attempt.observe(response.usage)
            await attempt.finish(
                "completed",
                reasons=[
                    choice.finish_reason
                    for choice in response.choices
                    if choice.finish_reason is not None
                ],
            )
            call.record.result.finish_reasons = attempt.record.result.finish_reasons.copy()
        return response
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await attempt.finish("cancelled")
        raise
    except Exception as exc:
        await attempt.finish("failed", exc=exc)
        raise
