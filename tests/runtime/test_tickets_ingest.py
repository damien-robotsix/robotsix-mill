"""Tests for ``POST /tickets/ingest`` — creation-time dedup endpoint."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.config import RepoConfig
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


# ---------------------------------------------------------------------------
# Auto-registered repo rejected when flag is off
# ---------------------------------------------------------------------------
def test_ingest_rejects_auto_repo_when_flag_off(client, settings):
    """POST /tickets/ingest for an auto-registered repo → 400 when the
    runtime registration flag is off."""
    # Add an auto-registered repo to the registry.
    auto_repo = RepoConfig(
        repo_id="auto-repo",
        board_id="auto-board",
        langfuse_project_name="",
        langfuse_public_key="",
        langfuse_secret_key="",
        forge_remote_url="https://github.com/x/y",
        source="auto",
    )
    client.app.state.repos.repos["auto-repo"] = auto_repo

    settings.allow_runtime_repo_registration = False
    payload = _ingest_payload(repo_id="auto-repo")
    r = client.post("/tickets/ingest", json=payload)
    assert r.status_code == 400
    assert "registered at runtime" in r.json()["detail"]


def test_ingest_accepts_auto_repo_when_flag_on(client, settings):
    """POST /tickets/ingest for an auto-registered repo → 201 when the
    runtime registration flag is on."""
    auto_repo = RepoConfig(
        repo_id="auto-repo-2",
        board_id="auto-board-2",
        langfuse_project_name="",
        langfuse_public_key="",
        langfuse_secret_key="",
        forge_remote_url="https://github.com/x/y",
        source="auto",
    )
    client.app.state.repos.repos["auto-repo-2"] = auto_repo

    settings.allow_runtime_repo_registration = True
    payload = _ingest_payload(repo_id="auto-repo-2")
    r = client.post("/tickets/ingest", json=payload)
    assert r.status_code == 201
    assert r.json()["deduped"] is False


# ---------------------------------------------------------------------------
# Synthetic meta board
# ---------------------------------------------------------------------------
def test_ingest_accepts_synthetic_meta_board(client):
    """POST /tickets/ingest with repo_id='meta' → 201 when the meta
    board is registered in repos.meta (not repos.repos)."""
    meta_config = RepoConfig(
        repo_id="meta",
        board_id="meta",
        langfuse_project_name="meta-project",
        langfuse_public_key="pk-meta",
        langfuse_secret_key="sk-meta",
    )
    client.app.state.repos.meta = meta_config

    payload = _ingest_payload(repo_id="meta")
    r = client.post("/tickets/ingest", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    assert body["ticket_id"]

    # Ticket is on the meta board.
    meta_svc = TicketService(client.app.state.settings, board_id="meta")
    ticket = meta_svc.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.board_id == "meta"
    assert ticket.title == "Test anomaly"


def test_ingest_meta_board_unknown_repo_id_still_rejected(client):
    """Unknown repo_ids that are NOT 'meta' still get 404."""
    r = client.post("/tickets/ingest", json=_ingest_payload(repo_id="not-meta"))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Normalized-title fingerprint dedup
# ---------------------------------------------------------------------------
def test_normalize_title_strips_timestamps():
    """_normalize_title strips ISO dates and timestamps from titles."""
    from robotsix_mill.runtime.routes._tickets_ingest import _normalize_title

    # Same symptom, different dates → same fingerprint.
    fp1 = _normalize_title("mail-ingester unhealthy on 2026-07-31")
    fp2 = _normalize_title("mail-ingester unhealthy on 2026-07-30")
    assert fp1 == fp2 == "mail-ingester unhealthy on"

    # Full ticket ID should be stripped.
    fp3 = _normalize_title(
        "no merge capability for repo reconcile 20260731T155119Z-slug-a3f2"
    )
    assert "20260731" not in fp3
    assert "slug" not in fp3


def test_normalize_title_strips_file_paths():
    """_normalize_title strips file paths with optional line numbers."""
    from robotsix_mill.runtime.routes._tickets_ingest import _normalize_title

    fp = _normalize_title("add docstring to src/foo/bar.py:123")
    assert "src/foo/bar.py" not in fp
    assert ":123" not in fp
    # Core symptom phrase should survive.
    assert "add docstring to" in fp


def test_normalize_title_case_folds():
    """_normalize_title case-folds input."""
    from robotsix_mill.runtime.routes._tickets_ingest import _normalize_title

    assert _normalize_title("MAIL-INGESTER UNHEALTHY") == _normalize_title(
        "mail-ingester unhealthy"
    )


def test_ingest_fingerprint_dedup_hit(client, service):
    """When the normalized title matches an existing open ticket, the
    endpoint returns 200 deduped=True without calling the LLM."""
    existing = service.create(
        "mail-ingester unhealthy on 2026-07-30",
        "The ingester container is failing health checks.",
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
                title="mail-ingester unhealthy on 2026-07-31",
                body="Still failing after restart.",
                source_tag="monitor-2",
            ),
        )
    # LLM dedup should NOT be called — fingerprint match is
    # deterministic and cheaper.
    assert mock_dedup.call_count == 0
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == existing.id
    assert body["deduped"] is True

    # History note appended.
    history = service.history(existing.id)
    notes = [e.note for e in history if e.note and "fingerprint match" in e.note]
    assert len(notes) == 1


def test_ingest_fingerprint_no_false_match(client, service):
    """Different symptoms with different normalized titles still reach the
    LLM dedup step (no fingerprint false-positive)."""
    _ = service.create(
        "mail-ingester unhealthy on 2026-07-30",
        "health check failure for the ingester service",
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
        r = client.post(
            "/tickets/ingest",
            json=_ingest_payload(
                title="database connection pool exhausted",
                body="Postgres max_connections reached for ingester service.",
            ),
        )
    # LLM dedup should be called since fingerprint didn't match AND
    # there is some token overlap ("ingester", "service").
    assert mock_dedup.call_count >= 1
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Concurrent-ingest race guard
# ---------------------------------------------------------------------------
def test_ingest_concurrent_identical_reports_create_one_ticket(client, service):
    """Two identical reports overlapping in time must yield ONE ticket.

    Regression guard for the duplicate-ticket incident: a retried
    ``POST /tickets/ingest`` had both attempts read the board before
    either created, so both missed the fingerprint and both created.
    The barrier below reproduces exactly that interleaving — each
    request is held inside the (slow) LLM dedup step until the other
    has also finished listing candidates.
    """
    # Seed an unrelated ticket that shares tokens, so both requests take
    # the LLM path rather than short-circuiting on "no candidates".
    service.create(
        "Something about deployment",
        "anomaly detection system",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    barrier = threading.Barrier(2, timeout=10)

    def _slow_dedup(**_kwargs) -> dict:
        # Both requests are now past their candidate listing.
        barrier.wait()
        return {"duplicate_of": None, "already_done": None, "reason": "distinct"}

    payload = _ingest_payload(title="Wallet value shows cash, not equity")
    results: list = [None, None]

    def _post(index: int) -> None:
        results[index] = client.post("/tickets/ingest", json=payload)

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
        side_effect=_slow_dedup,
    ):
        threads = [threading.Thread(target=_post, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 201], f"expected one create + one dedup, got {statuses}"

    ticket_ids = {r.json()["ticket_id"] for r in results}
    assert len(ticket_ids) == 1, "both requests must resolve to the same ticket"

    matching = [
        t for t in service.list() if t.title == "Wallet value shows cash, not equity"
    ]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Operational-maintenance classification
# ---------------------------------------------------------------------------
def test_ingest_rejects_operational_report(client, service):
    """A report classified as OPERATIONAL is rejected with structured
    reason and no ticket is created."""
    from robotsix_mill.agents.ops_classify import OpsClassifyVerdict

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_ops_classify_agent",
        return_value=OpsClassifyVerdict(
            classification="OPERATIONAL",
            reason="Manual credential rotation — no code change needed.",
        ),
    ), patch(
        "robotsix_mill.runtime.routes._tickets_ingest.emit_diagnostic_event",
        return_value=True,
    ):
        r = client.post(
            "/tickets/ingest",
            json=_ingest_payload(
                title="Rotate GHCR pull token due to cleartext transmission",
                body=(
                    "The GHCR pull token was transmitted in cleartext. "
                    "Rotate it manually via GitHub settings."
                ),
            ),
        )

    assert r.status_code == 200
    body = r.json()
    assert body["filed"] is False
    assert body["reason"] == "operational-maintenance"
    assert body["classification"] == "OPERATIONAL"
    assert "Manual credential rotation" in body["guidance"]

    # No ticket created.
    assert len(service.list()) == 0


def test_ingest_rejects_redeploy_report(client, service):
    """A service redeploy report classified as OPERATIONAL is rejected."""
    from robotsix_mill.agents.ops_classify import OpsClassifyVerdict

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_ops_classify_agent",
        return_value=OpsClassifyVerdict(
            classification="OPERATIONAL",
            reason="Service redeploy — no code change required.",
        ),
    ), patch(
        "robotsix_mill.runtime.routes._tickets_ingest.emit_diagnostic_event",
        return_value=True,
    ):
        r = client.post(
            "/tickets/ingest",
            json=_ingest_payload(
                title="Redeploy file-hub to activate llmio enrichment",
                body=(
                    "Redeploy file-hub to pick up the latest llmio enrichment "
                    "and Langfuse tracing."
                ),
            ),
        )

    assert r.status_code == 200
    body = r.json()
    assert body["filed"] is False
    assert body["reason"] == "operational-maintenance"


def test_ingest_allows_code_ticket_mentioning_deploy(client, service):
    """A ticket that mentions deploy/rotation but requires code changes
    is classified as CODE and proceeds normally."""
    from robotsix_mill.agents.ops_classify import OpsClassifyVerdict

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_ops_classify_agent",
        return_value=OpsClassifyVerdict(
            classification="CODE",
            reason=(
                "Report describes a code defect in the deploy script "
                "that must be fixed in the repository."
            ),
        ),
    ), patch(
        "robotsix_mill.runtime.routes._tickets_ingest.emit_diagnostic_event",
        return_value=True,
    ):
        r = client.post(
            "/tickets/ingest",
            json=_ingest_payload(
                title="Fix deploy script that fails to rotate tokens",
                body=(
                    "The deploy script crashes when trying to rotate "
                    "the GHCR token. Fix the rotation logic in "
                    "scripts/deploy.py."
                ),
            ),
        )

    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    assert body["classified"] == "CODE"

    # Ticket was created.
    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert "Fix deploy script" in ticket.title


def test_ingest_ops_classify_fail_open(client, service):
    """When the ops-classify LLM call fails, the report proceeds
    through normally (fail-open)."""
    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_ops_classify_agent",
        side_effect=RuntimeError("LLM timeout"),
    ):
        r = client.post("/tickets/ingest", json=_ingest_payload())

    # Fail-open: ticket is created.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None


def test_ingest_ops_classify_emits_diagnostic_event(client, service):
    """The ops-classify decision is recorded as a diagnostic event."""
    from robotsix_mill.agents.ops_classify import OpsClassifyVerdict

    emitted_events: list[dict] = []

    def _capture_emit(**kwargs):
        emitted_events.append(kwargs)
        return True

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_ops_classify_agent",
        return_value=OpsClassifyVerdict(
            classification="OPERATIONAL",
            reason="Token rotation.",
        ),
    ), patch(
        "robotsix_mill.runtime.routes._tickets_ingest.emit_diagnostic_event",
        side_effect=_capture_emit,
    ):
        r = client.post(
            "/tickets/ingest",
            json=_ingest_payload(
                title="Rotate PAT token",
                body="Rotate the PAT token.",
            ),
        )

    assert r.status_code == 200
    assert len(emitted_events) == 1
    event = emitted_events[0]
    assert event["category"] == "OPS_CLASSIFY"
    assert "OPERATIONAL" in event["reason"]


def test_ingest_ops_classify_runs_before_dedup(client, service):
    """The ops-classification step runs before dedup, so an
    operational report that matches an existing ticket is still
    rejected without being deduped."""
    from robotsix_mill.agents.ops_classify import OpsClassifyVerdict

    # Create an existing ticket with similar title.
    existing = service.create(
        "Rotate GHCR pull token",
        "Manual rotation needed.",
        source="monitor-1",
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    with patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_ops_classify_agent",
        return_value=OpsClassifyVerdict(
            classification="OPERATIONAL",
            reason="Manual credential rotation.",
        ),
    ), patch(
        "robotsix_mill.runtime.routes._tickets_ingest.emit_diagnostic_event",
        return_value=True,
    ), patch(
        "robotsix_mill.runtime.routes._tickets_ingest.run_dedup_check",
    ) as mock_dedup:
        r = client.post(
            "/tickets/ingest",
            json=_ingest_payload(
                title="Rotate GHCR pull token",
                body="Rotate the GHCR pull token.",
            ),
        )

    # Rejected as ops — dedup LLM was never called.
    assert r.status_code == 200
    body = r.json()
    assert body["filed"] is False
    assert body["reason"] == "operational-maintenance"
    mock_dedup.assert_not_called()
