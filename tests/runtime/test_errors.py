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


class TestProblemDetail:
    def test_minimal_valid(self) -> None:
        """Minimal construction: only required fields supplied."""
        detail = ProblemDetail(
            title="Not Found",
            status=404,
            detail="The requested resource was not found.",
        )
        assert detail.type == "about:blank"
        assert detail.title == "Not Found"
        assert detail.status == 404
        assert detail.detail == "The requested resource was not found."
        assert detail.instance is None
        assert detail.trace_id is None
        assert detail.errors is None

    def test_full_fields(self) -> None:
        """Construction with every field (including all Optionals)."""
        detail = ProblemDetail(
            type="https://example.com/errors/validation-error",
            title="Validation Error",
            status=422,
            detail="The request body failed validation.",
            instance="/tickets/abc123",
            trace_id="trace-456",
            errors=[{"field": "title", "message": "Title is required"}],
        )
        assert detail.type == "https://example.com/errors/validation-error"
        assert detail.title == "Validation Error"
        assert detail.status == 422
        assert detail.instance == "/tickets/abc123"
        assert detail.trace_id == "trace-456"
        assert detail.errors == [{"field": "title", "message": "Title is required"}]

    def test_default_type_is_about_blank(self) -> None:
        detail = ProblemDetail(title="Error", status=500, detail="Something went wrong.")
        assert detail.type == "about:blank"

    def test_status_must_be_at_least_100(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            ProblemDetail(title="Bad", status=99, detail="Too low.")

    def test_status_must_be_below_600(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            ProblemDetail(title="Bad", status=600, detail="Too high.")

    def test_status_100_is_valid(self) -> None:
        detail = ProblemDetail(title="Continue", status=100, detail="Continue.")
        assert detail.status == 100

    def test_status_599_is_valid(self) -> None:
        detail = ProblemDetail(title="OK", status=599, detail="OK.")
        assert detail.status == 599

    def test_missing_required_fields_raises(self) -> None:
        """title, status, detail are required (no default)."""
        with pytest.raises(ValidationError):
            ProblemDetail(title="Missing status and detail")  # type: ignore[call-arg]

        with pytest.raises(ValidationError):
            ProblemDetail(status=500, detail="Missing title")  # type: ignore[call-arg]

    def test_extra_fields_ignored(self) -> None:
        """Pydantic ignores unknown fields by default, so unknown kwargs
        should not appear in model_dump."""
        pd = ProblemDetail(
            title="OK",
            status=200,
            detail="all good",
            unknown_extra="should be ignored",  # type: ignore[call-arg]
        )
        dumped = pd.model_dump()
        assert "type" in dumped
        assert "unknown_extra" not in dumped

    def test_model_dump_includes_all_fields(self) -> None:
        detail = ProblemDetail(
            title="Teapot",
            status=418,
            detail="I'm a teapot",
            instance="/brew/1",
            trace_id="t-1",
            errors=None,
        )
        dumped: dict[str, Any] = detail.model_dump()
        assert dumped["type"] == "about:blank"
        assert dumped["title"] == "Teapot"
        assert dumped["status"] == 418
        assert dumped["detail"] == "I'm a teapot"
        assert dumped["instance"] == "/brew/1"
        assert dumped["trace_id"] == "t-1"
        assert dumped["errors"] is None

    def test_model_dump_matches_rfc9457_schema(self) -> None:
        """The dict produced by model_dump() contains every key that
        RFC 9457 §3.1 enumerates (type, title, status, detail, instance)."""
        pd = ProblemDetail(
            title="Not Found",
            status=404,
            detail="The ticket was not found.",
        )
        dumped = pd.model_dump()
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

    def test_model_dump_mode_json(self) -> None:
        detail = ProblemDetail(title="Err", status=500, detail="Boom")
        dumped: dict[str, Any] = detail.model_dump(mode="json")
        assert dumped["type"] == "about:blank"
        assert dumped["title"] == "Err"
        assert dumped["status"] == 500
        assert dumped["detail"] == "Boom"
        assert dumped["instance"] is None
        assert dumped["trace_id"] is None
        assert dumped["errors"] is None

    def test_model_dump_json_is_valid_rfc9457(self) -> None:
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

    def test_problem_detail_errors_field_roundtrips(self) -> None:
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
