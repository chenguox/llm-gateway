"""Request-local accounting state, with no request or response content retained."""

import hashlib
import hmac
import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

import anyio

from app.core.config import Settings
from app.core.errors import GatewayError, map_upstream_error
from app.governance.models import Error, InvocationRecord, Prompt, Result, Route, Timing, Usage
from app.governance.pricing import Cost, PriceBook, aggregate_cost, estimate
from app.governance.store import RecordStorageError, RecordStore
from app.schemas.chat import ChatCompletionUsage, LLMRequest
from app.schemas.models import ModelConfiguration

logger = logging.getLogger(__name__)
current_call: ContextVar["CallContext | None"] = ContextVar("current_call", default=None)


def usage_record(usage: ChatCompletionUsage | None, complete: bool) -> Usage:
    if usage is None:
        return Usage()
    known = usage.input_tokens is not None or usage.output_tokens is not None
    full = complete and usage.input_tokens is not None and usage.output_tokens is not None
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        known_input_tokens=usage.input_tokens,
        known_output_tokens=usage.output_tokens,
        status="complete" if full else "partial" if known else "missing",
        details=usage._details.copy(),
    )


class CallContext:
    def __init__(self, store: RecordStore, prices: PriceBook, settings: Settings) -> None:
        self.store = store
        self.prices = prices
        self.settings = settings
        self.started = perf_counter()
        call_id = uuid4().hex
        self.record = InvocationRecord(
            id=call_id,
            call_id=call_id,
            request_id=uuid4().hex,
            timing=Timing(started_at=datetime.now(UTC)),
        )
        self.stage = "validation"
        self.attempts: list[AttemptContext] = []
        self.active_attempt: AttemptContext | None = None
        self.write_failed = False
        self._finished = False

    async def start(self) -> None:
        try:
            await self.store.create(self.record)
        except RecordStorageError:
            logger.error(
                "CALL_RECORD_WRITE_FAILED request_id=%s stage=start", self.record.request_id
            )
            raise GatewayError(
                503, "CALL_RECORD_UNAVAILABLE", "Call record storage unavailable"
            ) from None

    async def persist(self, record: InvocationRecord) -> None:
        with anyio.CancelScope(shield=True):
            try:
                await self.store.save(record)
            except RecordStorageError:
                self.write_failed = True
                logger.error(
                    "CALL_RECORD_WRITE_FAILED request_id=%s record_id=%s stage=finalize",
                    self.record.request_id,
                    record.id,
                )

    def _prompt(self, request: LLMRequest, template: Prompt) -> Prompt:
        metadata = template.model_copy(deep=True)
        metadata.message_count = len(request.messages)
        metadata.character_count = sum(len(message.content) for message in request.messages)
        metadata.roles = {
            role: sum(message.role == role for message in request.messages)
            for role in ("system", "user", "assistant")
        }
        if self.settings.audit_hmac_key is not None:
            content = json.dumps(
                [message.model_dump() for message in request.messages],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            metadata.fingerprint = hmac.new(
                self.settings.audit_hmac_key.get_secret_value().encode("utf-8"),
                content,
                hashlib.sha256,
            ).hexdigest()
            metadata.fingerprint_key_id = self.settings.audit_hmac_key_id
        return metadata

    def capture_prompt(self, original: LLMRequest, built: LLMRequest) -> None:
        selection = original.system_prompt
        template = Prompt(
            source="template" if selection else "inline",
            name=selection.name if selection else None,
            version=selection.version if selection else None,
        )
        self.record.prompt = self._prompt(built, template)
        self.record.result.stream = built.stream

    def route(self, configuration: ModelConfiguration) -> None:
        self.record.route = Route(
            logical_model=configuration.logical_name,
            provider=configuration.provider,
            provider_model=configuration.model,
        )

    def error(self, code: str, stage: str | None = None, category: str | None = None) -> None:
        self.record.error = Error(code=code, stage=stage or self.stage, category=category)

    def terminal(
        self, status: str, *, reasons: list[str] | None = None, cancel_reason: str | None = None
    ) -> None:
        if self.record.result.status != "in_progress":
            return
        self.record.result.status = status
        self.record.result.finish_reasons = reasons or []
        if status == "cancelled":
            self.record.result.cancel_reason = cancel_reason or "request_cancelled"

    async def begin_attempt(self, request: LLMRequest, repair: bool) -> "AttemptContext":
        number = len(self.attempts) + 1
        record = InvocationRecord(
            id=uuid4().hex,
            call_id=self.record.call_id,
            request_id=self.record.request_id,
            attempt_number=number,
            route=self.record.route.model_copy(deep=True),
            prompt=self._prompt(request, self.record.prompt),
            timing=Timing(started_at=datetime.now(UTC)),
            result=Result(stream=request.stream),
        )
        record.resilience.attempt_count = 1
        record.resilience.repair_retry_count = int(repair)
        record.resilience.retry_reason = "structured_output_validation" if repair else None
        try:
            await self.store.save(self.record)
            await self.store.create(record)
        except RecordStorageError:
            logger.error(
                "CALL_RECORD_WRITE_FAILED request_id=%s stage=attempt_start", self.record.request_id
            )
            raise GatewayError(
                503, "CALL_RECORD_UNAVAILABLE", "Call record storage unavailable"
            ) from None
        attempt = AttemptContext(self, record)
        self.attempts.append(attempt)
        self.active_attempt = attempt
        return attempt

    async def assess(self, status: str, code: str | None = None) -> None:
        if self.active_attempt is None:
            return
        record = self.active_attempt.record
        self.record.result.validation_status = status
        record.result.validation_status = status
        if code is not None:
            record.error = Error(stage="structured_validation", code=code)
        await self.persist(record)

    async def finish(self, http_status: int | None) -> None:
        with anyio.CancelScope(shield=True):
            if self._finished:
                return
            self._finished = True
            for attempt in self.attempts:
                if attempt.record.result.status == "in_progress":
                    await attempt.finish(
                        "cancelled" if self.record.result.status == "cancelled" else "failed"
                    )
            if self.record.result.status == "in_progress":
                self.terminal(
                    "completed"
                    if http_status is not None and http_status < 400 and self.record.error is None
                    else "failed"
                )
            self.record.result.http_status = http_status
            self.record.timing.ended_at = datetime.now(UTC)
            self.record.timing.duration_ms = round((perf_counter() - self.started) * 1000, 3)
            records = [
                attempt.record
                for attempt in self.attempts
                if attempt.record.result.upstream_dispatched
            ]
            self.record.result.upstream_dispatched = bool(records)
            self.record.resilience.attempt_count = len(records)
            self.record.resilience.repair_retry_count = sum(
                record.resilience.repair_retry_count for record in records
            )
            self.record.resilience.timeout_count = sum(
                record.resilience.timeout_count for record in records
            )
            self.record.resilience.retry_reason = (
                "structured_output_validation"
                if self.record.resilience.repair_retry_count
                else None
            )
            complete = bool(records) and all(
                record.usage.status == "complete" for record in records
            )
            counters = {}
            known = {}
            for name in ("input_tokens", "output_tokens"):
                values = [getattr(record.usage, name) for record in records]
                final = bool(records) and all(
                    record.result.status == "completed" for record in records
                )
                counters[name] = (
                    sum(values) if final and all(value is not None for value in values) else None
                )
                reported = [getattr(record.usage, f"known_{name}") for record in records]
                known[f"known_{name}"] = (
                    sum(value for value in reported if value is not None)
                    if any(value is not None for value in reported)
                    else None
                )
            self.record.usage = Usage(
                **counters,
                **known,
                total_tokens=sum(counters.values())
                if all(value is not None for value in counters.values())
                else None,
                status="complete"
                if complete
                else "partial"
                if any(value is not None for value in known.values())
                else "missing",
            )
            self.record.result.reconciliation_required = self.write_failed or any(
                record.result.reconciliation_required for record in records
            )
            self.record.cost = aggregate_cost([record.cost for record in records])
            await self.persist(self.record)


class AttemptContext:
    def __init__(self, call: CallContext, record: InvocationRecord) -> None:
        self.call = call
        self.record = record
        self.started = perf_counter()
        self.usage: ChatCompletionUsage | None = None
        self.known: dict[str, int] = {}
        self.price = call.prices.select(
            record.route.provider, record.route.provider_model, record.timing.started_at
        )

    def observe(self, usage: ChatCompletionUsage | None) -> None:
        if usage is not None:
            for name in ("input_tokens", "output_tokens"):
                value = getattr(usage, name)
                if value is not None:
                    if value < self.known.get(name, 0):
                        raise GatewayError(
                            502, "UPSTREAM_INVALID_RESPONSE", "Invalid provider usage"
                        )
                    self.known[name] = value
            self.usage = usage
            self.record.usage = usage_record(usage, False)

    def first_content(self) -> None:
        if self.record.timing.first_content_ms is None:
            now = perf_counter()
            self.record.timing.first_content_ms = round((now - self.started) * 1000, 3)
            if self.call.record.timing.first_content_ms is None:
                self.call.record.timing.first_content_ms = round(
                    (now - self.call.started) * 1000, 3
                )

    async def finish(
        self, status: str, *, exc: Exception | None = None, reasons: list[str] | None = None
    ) -> None:
        if self.record.result.status != "in_progress":
            return
        self.record.result.status = status
        self.record.result.finish_reasons = reasons or []
        self.record.timing.ended_at = datetime.now(UTC)
        self.record.timing.duration_ms = round((perf_counter() - self.started) * 1000, 3)
        self.record.usage = usage_record(self.usage, status == "completed")
        self.record.usage.known_input_tokens = self.known.get("input_tokens")
        self.record.usage.known_output_tokens = self.known.get("output_tokens")
        self.record.result.reconciliation_required = self.record.usage.status != "complete"
        pricing_usage = self.usage
        if pricing_usage is not None:
            pricing_usage = pricing_usage.model_copy(
                update={
                    name: getattr(pricing_usage, name)
                    if getattr(pricing_usage, name) is not None
                    else self.known.get(name)
                    for name in ("input_tokens", "output_tokens")
                }
            )
        self.record.cost = estimate(
            self.price, pricing_usage, self.record.usage.status == "complete"
        )
        if not self.record.result.upstream_dispatched:
            self.record.resilience.attempt_count = 0
            self.record.resilience.repair_retry_count = 0
            self.record.result.reconciliation_required = False
            self.record.cost = Cost(status="not_incurred", amount=0, known_amount=0)
        if status == "cancelled":
            self.record.result.cancel_reason = (
                self.call.record.result.cancel_reason or "request_cancelled"
            )
        if exc is not None:
            error = map_upstream_error(exc)
            code = error.code if error else "INTERNAL_ERROR"
            self.record.error = Error(stage="upstream", code=code, category=type(exc).__name__)
            self.record.resilience.timeout_count = int(code == "UPSTREAM_TIMEOUT")
        await self.call.persist(self.record)


def capture_headers(headers, status: int) -> None:
    call = current_call.get()
    if call is None or call.active_attempt is None:
        return
    record = call.active_attempt.record
    record.result.http_status = status
    value = headers.get("x-request-id") or headers.get("request-id")
    if value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        record.upstream_request_id = value


def capture_usage(usage: ChatCompletionUsage | None) -> None:
    call = current_call.get()
    if call is not None and call.active_attempt is not None:
        call.active_attempt.observe(usage)
