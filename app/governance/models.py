"""Allowlisted records; request and response bodies are intentionally absent."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.governance.pricing import Cost


class Route(BaseModel):
    logical_model: str | None = None
    provider: str | None = None
    provider_model: str | None = None


class Prompt(BaseModel):
    source: Literal["unresolved", "inline", "template"] = "unresolved"
    name: str | None = None
    version: str | None = None
    message_count: int = 0
    character_count: int = 0
    roles: dict[str, int] = Field(default_factory=dict)
    fingerprint: str | None = None
    fingerprint_key_id: str | None = None


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    known_input_tokens: int | None = None
    known_output_tokens: int | None = None
    status: Literal["complete", "partial", "missing"] = "missing"
    details: dict[str, int | None] = Field(default_factory=dict)


class Timing(BaseModel):
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    first_content_ms: float | None = None


class Resilience(BaseModel):
    attempt_count: int = 0
    repair_retry_count: int = 0
    timeout_count: int = 0
    fallback_used: bool = False
    retry_reason: Literal["structured_output_validation"] | None = None


class Result(BaseModel):
    status: Literal["in_progress", "completed", "failed", "cancelled"] = "in_progress"
    stream: bool | None = None
    http_status: int | None = None
    finish_reasons: list[str] = Field(default_factory=list)
    validation_status: Literal["not_requested", "passed", "failed", "refused"] = "not_requested"
    cancel_reason: Literal["client_disconnected", "request_cancelled"] | None = None
    reconciliation_required: bool = True
    upstream_dispatched: bool = False


class Error(BaseModel):
    stage: str
    code: str
    category: str | None = None


class InvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    call_id: str
    request_id: str
    attempt_number: int | None = None
    stream_id: str | None = None
    upstream_request_id: str | None = None
    route: Route = Field(default_factory=Route)
    prompt: Prompt = Field(default_factory=Prompt)
    usage: Usage = Field(default_factory=Usage)
    timing: Timing
    resilience: Resilience = Field(default_factory=Resilience)
    result: Result = Field(default_factory=Result)
    error: Error | None = None
    cost: Cost = Field(default_factory=Cost)
