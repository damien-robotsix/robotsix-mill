"""Tests for ``POST /tickets/ingest`` — creation-time dedup endpoint."""

from __future__ import annotations

import threading

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
# Dedup hit — now handled by classify stage
# ---------------------------------------------------------------------------
def test_ingest_dedup_hit(client, service):
    """When fingerprint dedup matches, the endpoint returns 200,
    deduped=True. LLM dedup is now handled by the classify stage."""
    existing = service.create(
        "mail-ingester unhealthy on 2026-07-30",
        "Something went wrong with the deployment.",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )
    r = client.post(
        "/tickets/ingest",
        json=_ingest_payload(
            title="mail-ingester unhealthy on 2026-07-31",
            body="Still failing after restart.",
        ),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == existing.id
    assert body["deduped"] is True


# ---------------------------------------------------------------------------
# Dedup miss — ticket created in CLASSIFYING state
# ---------------------------------------------------------------------------
def test_ingest_dedup_miss(client, service):
    """When no fingerprint match, the endpoint creates a ticket in
    CLASSIFYING state for async classification."""
    r = client.post("/tickets/ingest", json=_ingest_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    assert body["ticket_id"]

    # Ticket exists in the DB in CLASSIFYING state.
    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.title == "Test anomaly"
    assert ticket.state == "classifying"


# ---------------------------------------------------------------------------
# LLM failure → fail-open (now handled by classify stage)
# ---------------------------------------------------------------------------
def test_ingest_llm_failure_fail_open(client, service):
    """The ingest route no longer runs LLM dedup inline — it creates
    tickets in CLASSIFYING state for async classification."""
    r = client.post("/tickets/ingest", json=_ingest_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False
    assert body["ticket_id"]

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


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
# No overlap → ticket created in CLASSIFYING state
# ---------------------------------------------------------------------------
def test_ingest_no_overlap_skips_llm(client, service):
    """The ingest route creates tickets in CLASSIFYING state regardless
    of token overlap — LLM dedup is now handled by the classify stage."""
    service.create(
        "12345 67890",
        "99999 00000",  # all digits
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    r = client.post(
        "/tickets/ingest",
        json=_ingest_payload(
            title="abcdef ghijkl",
            body="mnopqr stuvwx",  # all letters — zero overlap
        ),
    )
    assert r.status_code == 201
    assert r.json()["deduped"] is False

    # Ticket created in CLASSIFYING state.
    ticket = service.get(r.json()["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


# ---------------------------------------------------------------------------
# No candidates → ticket created in CLASSIFYING state
# ---------------------------------------------------------------------------
def test_ingest_no_candidates_skips_llm(client):
    """When the board has zero tickets, the ingest route creates a ticket
    in CLASSIFYING state — LLM dedup is handled by the classify stage."""
    r = client.post("/tickets/ingest", json=_ingest_payload())
    assert r.status_code == 201
    assert r.json()["deduped"] is False

    # Ticket created in CLASSIFYING state.
    from robotsix_mill.core.service import TicketService

    svc = TicketService(client.app.state.settings, board_id="test-board")
    ticket = svc.get(r.json()["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


# ---------------------------------------------------------------------------
# already_done is ignored (treated as negative) — now handled by classify stage
# ---------------------------------------------------------------------------
def test_ingest_already_done_treated_as_negative(client, service):
    """The ingest route creates tickets in CLASSIFYING state regardless
    of already_done — LLM dedup is now handled by the classify stage."""
    r = client.post("/tickets/ingest", json=_ingest_payload())
    assert r.status_code == 201
    assert r.json()["deduped"] is False

    # Ticket created in CLASSIFYING state.
    ticket = service.get(r.json()["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


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
    endpoint returns 200 deduped=True. LLM dedup is not called —
    fingerprint match is deterministic and handled entirely in the
    ingest route (no LLM import remains in the module)."""
    existing = service.create(
        "mail-ingester unhealthy on 2026-07-30",
        "The ingester container is failing health checks.",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    r = client.post(
        "/tickets/ingest",
        json=_ingest_payload(
            title="mail-ingester unhealthy on 2026-07-31",
            body="Still failing after restart.",
            source_tag="monitor-2",
        ),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == existing.id
    assert body["deduped"] is True

    # History note appended.
    history = service.history(existing.id)
    notes = [e.note for e in history if e.note and "fingerprint match" in e.note]
    assert len(notes) == 1


def test_ingest_fingerprint_no_false_match(client, service):
    """Different symptoms with different normalized titles still create
    a ticket in CLASSIFYING state (no fingerprint false-positive)."""
    _ = service.create(
        "mail-ingester unhealthy on 2026-07-30",
        "health check failure for the ingester service",
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    r = client.post(
        "/tickets/ingest",
        json=_ingest_payload(
            title="database connection pool exhausted",
            body="Postgres max_connections reached for ingester service.",
        ),
    )
    # No fingerprint match — ticket created in CLASSIFYING state.
    assert r.status_code == 201
    ticket = service.get(r.json()["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


# ---------------------------------------------------------------------------
# Concurrent-ingest race guard
# ---------------------------------------------------------------------------
def test_ingest_concurrent_identical_reports_create_one_ticket(client, service):
    """Two identical reports overlapping in time must yield ONE ticket.

    Regression guard for the duplicate-ticket incident: a retried
    ``POST /tickets/ingest`` had both attempts read the board before
    either created, so both missed the fingerprint and both created.
    The board lock in ``_create_ticket_guarded`` closes the window.
    """
    payload = _ingest_payload(title="Wallet value shows cash, not equity")
    results: list = [None, None]

    def _post(index: int) -> None:
        results[index] = client.post("/tickets/ingest", json=payload)

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
# Operational-maintenance classification — now handled by classify stage
# ---------------------------------------------------------------------------
def test_ingest_rejects_operational_report(client, service):
    """The ingest route creates tickets in CLASSIFYING state — ops
    classification is now handled by the classify stage."""
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

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


def test_ingest_rejects_redeploy_report(client, service):
    """The ingest route creates tickets in CLASSIFYING state — ops
    classification is now handled by the classify stage."""
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

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


def test_ingest_allows_code_ticket_mentioning_deploy(client, service):
    """The ingest route creates tickets in CLASSIFYING state — ops
    classification is now handled by the classify stage."""
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

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


def test_ingest_ops_classify_fail_open(client, service):
    """The ingest route creates tickets in CLASSIFYING state — ops
    classification is now handled by the classify stage."""
    r = client.post("/tickets/ingest", json=_ingest_payload())

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


def test_ingest_ops_classify_emits_diagnostic_event(client, service):
    """The ingest route creates tickets in CLASSIFYING state — diagnostic
    events are now emitted by the classify stage."""
    r = client.post(
        "/tickets/ingest",
        json=_ingest_payload(
            title="Rotate PAT token",
            body="Rotate the PAT token.",
        ),
    )

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"


def test_ingest_ops_classify_runs_before_dedup(client, service):
    """The ingest route creates tickets in CLASSIFYING state — both ops
    classification and dedup are now handled by the classify stage."""
    # Create an existing ticket with similar title.
    service.create(
        "Rotate GHCR pull token",
        "Manual rotation needed.",
        source="monitor-1",
        kind=TicketKind.TASK,
        board_id="test-board",
    )

    r = client.post(
        "/tickets/ingest",
        json=_ingest_payload(
            title="Rotate GHCR pull token",
            body="Rotate the GHCR pull token.",
        ),
    )

    # Fingerprint match — deduped at ingest time.
    assert r.status_code == 200
    body = r.json()
    assert body["deduped"] is True


# ---------------------------------------------------------------------------
# Scope classification / auto-epic promotion — now handled by classify stage
# ---------------------------------------------------------------------------
def _scope_verdict(classification: str, confidence: float, reason: str = "r"):
    from robotsix_mill.agents.scope_classify import ScopeVerdict

    return ScopeVerdict(
        classification=classification, confidence=confidence, reason=reason
    )


def _broad_payload() -> dict:
    return _ingest_payload(
        title="Build the whole notifications subsystem",
        body=(
            "Add email, SMS, and webhook delivery channels plus a "
            "user preferences UI and a retry/backoff scheduler."
        ),
    )


def test_ingest_promotes_broad_report_to_epic(client, service):
    """The ingest route creates tickets in CLASSIFYING state — epic
    promotion is now handled by the classify stage."""
    r = client.post("/tickets/ingest", json=_broad_payload())

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"
    assert ticket.kind == TicketKind.TASK


def test_ingest_narrow_report_stays_task(client, service):
    """The ingest route creates tickets in CLASSIFYING state — scope
    classification is now handled by the classify stage."""
    r = client.post("/tickets/ingest", json=_ingest_payload())

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    body = r.json()
    assert body["deduped"] is False

    ticket = service.get(body["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"
    assert ticket.kind == TicketKind.TASK


def test_ingest_borderline_epic_below_threshold_stays_task(client, service):
    """The ingest route creates tickets in CLASSIFYING state — scope
    classification is now handled by the classify stage."""
    r = client.post("/tickets/ingest", json=_broad_payload())

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    ticket = service.get(r.json()["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"
    assert ticket.kind == TicketKind.TASK


def test_ingest_scope_classify_disabled(client, service, settings):
    """The ingest route creates tickets in CLASSIFYING state regardless
    of auto_epic_enabled — scope classification is now handled by the
    classify stage."""
    settings.auto_epic_enabled = False
    r = client.post("/tickets/ingest", json=_broad_payload())

    # Ticket created in CLASSIFYING state — classification happens async.
    assert r.status_code == 201
    ticket = service.get(r.json()["ticket_id"])
    assert ticket is not None
    assert ticket.state == "classifying"
    assert ticket.kind == TicketKind.TASK


def test_ingest_reingest_epic_is_idempotent(client, service):
    """Re-ingesting the same broad report is deduped by the title
    fingerprint — the scope classifier is not re-run and no new
    children are created (idempotency preserved)."""
    # First ingest — creates ticket in CLASSIFYING state.
    r1 = client.post("/tickets/ingest", json=_broad_payload())
    assert r1.status_code == 201
    ticket_id = r1.json()["ticket_id"]

    # Second identical ingest — fingerprint match, deduped.
    r2 = client.post("/tickets/ingest", json=_broad_payload())
    assert r2.status_code == 200
    assert r2.json()["deduped"] is True
    assert r2.json()["ticket_id"] == ticket_id
