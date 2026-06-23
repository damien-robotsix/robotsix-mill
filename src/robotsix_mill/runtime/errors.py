"""Structured error response models (RFC 9457 Problem Details)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProblemDetail(BaseModel):
    """RFC 9457 Problem Details JSON envelope."""

    type: str = Field(
        default="about:blank", description="URI identifying the error type"
    )
    title: str = Field(description="Short, human-readable summary of the problem type")
    status: int = Field(description="HTTP status code", ge=100, lt=600)
    detail: str = Field(
        description="Human-readable explanation specific to this occurrence"
    )
    instance: str | None = Field(
        default=None,
        description="URI identifying the specific occurrence of the problem",
    )
    trace_id: str | None = Field(
        default=None, description="OpenTelemetry trace ID for correlating with logs"
    )
    errors: list[dict[str, Any]] | None = Field(
        default=None, description="Field-level validation errors (422 only)"
    )
