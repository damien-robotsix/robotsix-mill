"""Unit tests for ``ProblemDetail`` — the RFC 9457 error envelope.

Covers construction, defaults, validation, and serialisation.
The integration with FastAPI exception handlers is already
exercised in ``test_exception_handlers.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from robotsix_mill.runtime.errors import ProblemDetail


def test_defaults() -> None:
    """Minimal construction: only required fields supplied."""
    pd = ProblemDetail(
        title="Bad Request",
        status=400,
        detail="Missing required field 'q'",
    )
    assert pd.type == "about:blank"
    assert pd.title == "Bad Request"
    assert pd.status == 400
    assert pd.detail == "Missing required field 'q'"
    assert pd.instance is None
    assert pd.trace_id is None
    assert pd.errors is None


def test_all_fields() -> None:
    """Construction with every field (including all Optionals)."""
    pd = ProblemDetail(
        type="https://example.com/errors/out-of-credits",
        title="Payment Required",
        status=402,
        detail="Your account balance is too low to process this request.",
        instance="/tickets/abc123",
        trace_id="0af7651916cd43dd8448eb211c80319c",
        errors=[{"loc": ["body", "q"], "msg": "field required", "type": "missing"}],
    )
    assert pd.type == "https://example.com/errors/out-of-credits"
    assert pd.title == "Payment Required"
    assert pd.status == 402
    assert pd.detail.startswith("Your account balance")
    assert pd.instance == "/tickets/abc123"
    assert pd.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert len(pd.errors) == 1  # type: ignore[arg-type]
    assert pd.errors[0]["loc"] == ["body", "q"]  # type: ignore[index]


def test_status_out_of_range_raises() -> None:
    """status must be >= 100 and < 600 (ge=100, lt=600)."""
    with pytest.raises(ValidationError, match="status"):
        ProblemDetail(title="Nope", status=99, detail="out of range low")

    with pytest.raises(ValidationError, match="status"):
        ProblemDetail(title="Nope", status=600, detail="out of range high")


def test_missing_required_fields_raises() -> None:
    """title, status, detail are required (no default)."""
    with pytest.raises(ValidationError):
        ProblemDetail(title="Missing status and detail")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        ProblemDetail(status=500, detail="Missing title")  # type: ignore[call-arg]


def test_extra_fields_ignored() -> None:
    """Pydantic ignores unknown fields by default (no extra='forbid'), so
    unknown kwargs should not appear in model_dump."""
    pd = ProblemDetail(
        title="OK",
        status=200,
        detail="all good",
        unknown_extra="should be ignored",  # type: ignore[call-arg]
    )
    dumped = pd.model_dump()
    assert "type" in dumped
    assert "unknown_extra" not in dumped


def test_model_dump_matches_rfc9457_schema() -> None:
    """The dict produced by model_dump() contains every key that
    RFC 9457 §3.1 enumerates (type, title, status, detail, instance)."""
    pd = ProblemDetail(
        title="Not Found",
        status=404,
        detail="The ticket was not found.",
    )
    dumped = pd.model_dump()
    # All RFC 9457 keys present:
    assert set(dumped.keys()) == {
        "type",
        "title",
        "status",
        "detail",
        "instance",
        "trace_id",
        "errors",
    }
    assert dumped["type"] == "about:blank"
    assert dumped["title"] == "Not Found"
    assert dumped["status"] == 404
    assert dumped["detail"] == "The ticket was not found."
    assert dumped["instance"] is None
    assert dumped["trace_id"] is None
    assert dumped["errors"] is None


def test_model_dump_json_is_valid_rfc9457() -> None:
    """model_dump_json() produces JSON that matches the RFC 9457 shape."""
    pd = ProblemDetail(
        title="Conflict",
        status=409,
        detail="The resource has changed; please retry.",
        instance="/tickets/42",
        trace_id="0af7651916cd43dd8448eb211c80319c",
    )
    raw = pd.model_dump_json()
    body: dict[str, Any] = json.loads(raw)
    assert body["type"] == "about:blank"
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert body["detail"] == "The resource has changed; please retry."
    assert body["instance"] == "/tickets/42"
    assert body["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert body["errors"] is None


# ---------------------------------------------------------------------------
# Integration: the handlers in test_exception_handlers.py already
# confirm ProblemDetail flows through FastAPI to the wire.  Add one
# more variant here that exercises the *validation* handler path
# (errors=...) so the ``errors`` field on ProblemDetail is confirmed
# to round-trip through jsonable_encoder / model_dump correctly.
# ---------------------------------------------------------------------------


def test_problem_detail_errors_field_roundtrips() -> None:
    """Pydantic validation error dicts serialize cleanly through ProblemDetail."""
    pd = ProblemDetail(
        title="Unprocessable Entity",
        status=422,
        detail="Request validation failed",
        errors=[
            {
                "loc": ["body", "repos"],
                "msg": "field required",
                "type": "missing",
            },
            {
                "loc": ["query", "limit"],
                "msg": "ensure this value is less than 100",
                "type": "value_error",
            },
        ],
    )
    dumped = pd.model_dump()
    assert len(dumped["errors"]) == 2  # type: ignore[arg-type]
    first = dumped["errors"][0]  # type: ignore[index]
    assert first["loc"] == ["body", "repos"]
    assert first["msg"] == "field required"
    assert first["type"] == "missing"

    # JSON round-trip
    reloaded = ProblemDetail.model_validate(json.loads(pd.model_dump_json()))
    assert reloaded.errors == dumped["errors"]
