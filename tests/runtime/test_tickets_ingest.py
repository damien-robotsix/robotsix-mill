"""Tests for ``POST /tickets/ingest`` — creation-time dedup endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.runtime.api import create_app


@pytest.fixture
def service(settings) -> TicketService:
    """Return the board-scoped service for the test board."""
    return TicketService(settings, board_id="test-board")


@pytest.fixture
def client(settings, repos_registry):
    """TestClient wired to the single-repo test app."""
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


def _ingest_payload(**overrides) -> dict:
    """Build an ingest payload with sensible defaults."""
    data: dict = {
        "repo_id": "test-repo",
        "title": "Test anomaly",
        "body": "Something went wrong with the deployment.",
        "source_tag": "monitor-1",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Dedup hit
# ---------------------------------------------------------------------------
def test_ingest_dedup_hit(client, service):
    """When run_dedup_check returns duplicate_of, the endpoint returns
    200, deduped=True, and appends a history note to the existing ticket."""
    existing = service.create(
        "Existing anomaly",
        "Something went wrong with the deployment.",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )
    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
        return_value={
            "duplicate_of": existing.id,
            "already_done": None,
            "reason": "same anomaly",
        },
    ) as mock_dedup:
        r = client.post("/tickets/ingest", json=_ingest_payload())
    assert mock_dedup.called
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == existing.id
    assert body["deduped"] is True

    # History note appended.
    history = service.history(existing.id)
    notes = [e.note for e in history if e.note and "re-reported by" in e.note]
    assert len(notes) == 1
    assert "monitor-1" in notes[0]


# ---------------------------------------------------------------------------
# Dedup miss
# ---------------------------------------------------------------------------
def test_ingest_dedup_miss(client, service):
    """When run_dedup_check returns no duplicate_of, the endpoint returns
    201, deduped=False, and a new ticket is created."""
    # Seed a ticket that shares tokens so candidates are selected for LLM dedup.
    service.create(
        "Something about deployment",
        "anomaly detection system",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
        return_value={
            "duplicate_of": None,
            "already_done": None,
            "reason": "different",
        },
    ) as mock_dedup:
        r = client.post("/tickets/ingest", json=_ingest_payload())
    assert mock_dedup.called
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    assert body["ticket_id"]

    # Ticket exists in the DB.
    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.title == "Test anomaly"


# ---------------------------------------------------------------------------
# LLM failure → fail-open
# ---------------------------------------------------------------------------
def test_ingest_llm_failure_fail_open(client, service):
    """When run_dedup_check raises, the endpoint still creates the ticket
    (fail-open — a missed dedup is cheaper than a lost incident report)."""
    # Seed so we pass the candidate check and hit the LLM path.
    service.create(
        "Existing ticket",
        "deployment went wrong",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
        side_effect=RuntimeError("timeout"),
    ) as mock_dedup:
        r = client.post("/tickets/ingest", json=_ingest_payload())
    assert mock_dedup.called
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    assert body["ticket_id"]

    ticket = service.get(body["ticket_id"])
    assert ticket is not None


# ---------------------------------------------------------------------------
# Unknown repo_id → 404
# ---------------------------------------------------------------------------
def test_ingest_unknown_repo_id(client):
    """POST with an unregistered repo_id returns 404."""
    r = client.post("/tickets/ingest", json=_ingest_payload(repo_id="does-not-exist"))
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "does-not-exist" in detail


# ---------------------------------------------------------------------------
# No overlap → skip LLM
# ---------------------------------------------------------------------------
def test_ingest_no_overlap_skips_llm(client, service):
    """When the draft shares zero tokens with any candidate, run_dedup_check
    is never called and the ticket is created directly."""
    service.create(
        "12345 67890",
        "99999 00000",  # all digits
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
    ) as mock_dedup:
        r = client.post(
            "/tickets/ingest",
            json=_ingest_payload(
                title="abcdef ghijkl",
                body="mnopqr stuvwx",  # all letters — zero overlap
            ),
        )
    assert mock_dedup.call_count == 0
    assert r.status_code == 201
    assert r.json()["deduped"] is False


# ---------------------------------------------------------------------------
# No candidates → skip LLM
# ---------------------------------------------------------------------------
def test_ingest_no_candidates_skips_llm(client):
    """When the board has zero tickets, run_dedup_check is never called."""
    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
    ) as mock_dedup:
        r = client.post("/tickets/ingest", json=_ingest_payload())
    assert mock_dedup.call_count == 0
    assert r.status_code == 201
    assert r.json()["deduped"] is False


# ---------------------------------------------------------------------------
# already_done is ignored (treated as negative)
# ---------------------------------------------------------------------------
def test_ingest_already_done_treated_as_negative(client, service):
    """The already_done verdict has no effect — it falls through to create."""
    service.create(
        "Existing ticket",
        "deployment went wrong",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
        return_value={
            "duplicate_of": None,
            "already_done": "some-ticket-id",
            "reason": "already implemented",
        },
    ) as mock_dedup:
        r = client.post("/tickets/ingest", json=_ingest_payload())
    assert mock_dedup.called
    assert r.status_code == 201
    assert r.json()["deduped"] is False
