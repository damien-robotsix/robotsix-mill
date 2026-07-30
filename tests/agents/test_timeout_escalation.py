"""Tests for the timeout-escalation runner and worker wiring."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import Comment, SourceKind, Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _make_ticket(
    service: TicketService,
    settings: Settings,
    title: str = "test-ticket",
    state: State = State.AWAITING_USER_REPLY,
    updated_at_delta: timedelta | None = None,
) -> Ticket:
    """Create a ticket and optionally set its state + updated_at via direct DB write."""
    t = service.create(title, source=SourceKind.AGENT)
    with db.session(settings, service.board_id) as s:
        row = s.get(Ticket, t.id)
        row.state = state
        if state == State.AWAITING_USER_REPLY:
            row.paused_from = State.READY.value
        if updated_at_delta is not None:
            row.updated_at = datetime.now(timezone.utc) - updated_at_delta
        s.add(row)
        s.commit()
    return service.get(t.id)


def _add_ask_user_thread(
    service: TicketService,
    ticket_id: str,
    closed: bool = False,
) -> Comment:
    """Add a top-level [ASK_USER] comment thread.  Optionally close it."""
    c = service.add_comment(
        ticket_id,
        "[ASK_USER] operator clarification needed",
        author="system",
    )
    if closed:
        with db.session(service.settings, service.board_id) as s:
            row = s.get(Comment, c.id)
            row.closed_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
    return c


# ---------------------------------------------------------------------------
# Runner-level tests (AC5.1–AC5.5)
# ---------------------------------------------------------------------------


def test_older_than_threshold_escalates(settings, service, monkeypatch):
    """AC5.1: Ticket older than threshold → escalated to BLOCKED."""
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 86400  # 1 day
    t = _make_ticket(service, settings, updated_at_delta=timedelta(days=4))
    _add_ask_user_thread(service, t.id, closed=True)  # already closed thread

    result = run_timeout_escalation(settings)
    assert result["escaped"] == 1

    updated = service.get(t.id)
    assert updated.state is State.BLOCKED

    # Check the system comment was added.
    comments = service.list_comments(t.id)
    system_comments = [
        c
        for c in comments
        if c.author == "system" and "escalated" in (c.body or "").lower()
    ]
    assert len(system_comments) >= 1

    # Check TicketEvent with the note.
    events = service.history(t.id)
    block_events = [e for e in events if e.state == State.BLOCKED]
    assert len(block_events) == 1
    assert "Escalated to BLOCKED" in (block_events[0].note or "")

    # Notification fired.
    assert len(notifications) == 1
    assert notifications[0][1] == State.BLOCKED


def test_newer_than_threshold_untouched(settings, service, monkeypatch):
    """AC5.2: Ticket newer than threshold → untouched."""
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 259200  # 3 days
    t = _make_ticket(service, settings, updated_at_delta=timedelta(hours=1))
    _add_ask_user_thread(service, t.id)

    result = run_timeout_escalation(settings)
    assert result["escaped"] == 0
    assert result["skipped"] == 0

    updated = service.get(t.id)
    assert updated.state is State.AWAITING_USER_REPLY
    assert len(notifications) == 0


def test_operator_reply_auto_resumes_ticket_skipped(settings, service, monkeypatch):
    """AC5.3: Ticket with operator reply → auto-resumed, no longer a
    candidate for timeout escalation.

    Posting a reply to an open [ASK_USER] thread on an
    AWAITING_USER_REPLY ticket now auto-closes the thread and resumes
    the ticket — the runner never sees it (neither escalated nor
    skipped).
    """
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 86400  # 1 day
    t = _make_ticket(service, settings, updated_at_delta=timedelta(days=4))
    thread = _add_ask_user_thread(service, t.id)
    # Add a child reply — operator responded.  This auto-closes the
    # [ASK_USER] thread and auto-resumes the ticket to READY.
    service.add_comment(
        t.id, "Here's the info you asked for", author="user", parent_id=thread.id
    )

    result = run_timeout_escalation(settings)
    assert result["escaped"] == 0
    assert result["skipped"] == 0

    updated = service.get(t.id)
    assert updated.state is State.READY
    assert len(notifications) == 0


def test_threshold_zero_noops(settings, service, monkeypatch):
    """AC5.4: Threshold ≤ 0 → pass no-ops."""
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 0
    t = _make_ticket(service, settings, updated_at_delta=timedelta(days=4))
    _add_ask_user_thread(service, t.id, closed=True)

    result = run_timeout_escalation(settings)
    assert result["escaped"] == 0
    assert result["skipped"] == 0

    updated = service.get(t.id)
    assert updated.state is State.AWAITING_USER_REPLY
    assert len(notifications) == 0


def test_already_blocked_skipped_with_warning(settings, service, caplog, monkeypatch):
    """AC5.5: Already BLOCKED ticket → skipped with warning, no error propagates."""
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )
    from robotsix_mill.core.service import TicketService

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 86400  # 1 day

    t = _make_ticket(service, settings, updated_at_delta=timedelta(days=4))
    _add_ask_user_thread(service, t.id, closed=True)

    # Monkeypatch TicketService.transition at the class level so the
    # runner's own service instance is affected.

    def racing_transition(self, ticket_id, dst, note=None):
        raise __import__(
            "robotsix_mill.core.service", fromlist=["TransitionError"]
        ).TransitionError(
            f"{ticket_id}: {State.AWAITING_USER_REPLY} -> {dst} not allowed"
        )

    monkeypatch.setattr(TicketService, "transition", racing_transition)

    import logging

    with caplog.at_level(logging.WARNING):
        result = run_timeout_escalation(settings)

    assert result["escaped"] == 0
    assert result["skipped"] == 1
    assert "failed to escalate" in caplog.text.lower()
    assert len(notifications) == 0


def test_no_ask_thread_still_escalates(settings, service, monkeypatch):
    """Ticket without any [ASK_USER] comment thread should still be escalated."""
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 86400
    t = _make_ticket(service, settings, updated_at_delta=timedelta(days=4))
    # No [ASK_USER] thread at all.

    result = run_timeout_escalation(settings)
    assert result["escaped"] == 1

    updated = service.get(t.id)
    assert updated.state is State.BLOCKED
    assert len(notifications) == 1


# ---------------------------------------------------------------------------
# Worker task-creation tests (AC5.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_timeout_escalation_task_created_when_periodic(
    tmp_path, monkeypatch, repo_config
):
    """Worker._timeout_escalation_task is created when timeout_escalation_periodic=true."""
    from robotsix_mill.stages import StageContext
    from robotsix_mill.runtime.worker import Worker
    from robotsix_mill.core.service import TicketService

    s = Settings(
        data_dir=str(tmp_path / "data"),
        timeout_escalation_periodic="true",
        timeout_escalation_interval_seconds="1",
    )
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    service = TicketService(s, board_id="test-board")
    ctx = StageContext(settings=s, service=service, repo_config=repo_config)

    # Patch _timeout_escalation_poll_loop to be a no-op.
    async def noop_poll(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(Worker, "_timeout_escalation_poll_loop", noop_poll)

    worker = Worker(ctx)
    worker.start()

    assert worker._timeout_escalation_task is not None
    assert not worker._timeout_escalation_task.done()

    await worker.stop()
    db.reset_engine()


@pytest.mark.asyncio
async def test_worker_timeout_escalation_task_not_created_when_periodic_false(
    tmp_path, monkeypatch, repo_config
):
    """Worker._timeout_escalation_task is NOT created when timeout_escalation_periodic=false."""
    from robotsix_mill.stages import StageContext
    from robotsix_mill.runtime.worker import Worker
    from robotsix_mill.core.service import TicketService

    s = Settings(
        data_dir=str(tmp_path / "data"),
        timeout_escalation_periodic="false",
    )
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    service = TicketService(s, board_id="test-board")
    ctx = StageContext(settings=s, service=service, repo_config=repo_config)

    worker = Worker(ctx)
    worker.start()

    assert worker._timeout_escalation_task is None

    await worker.stop()
    db.reset_engine()


# ---------------------------------------------------------------------------
# ask_user timeout → blocked → answered → resumed round-trip
# ---------------------------------------------------------------------------


def test_ask_user_timeout_blocked_answered_resumed(settings, service, monkeypatch):
    """Full round-trip: ask_user timeout → BLOCKED → answer → resumed to paused_from."""
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 86400  # 1 day

    # 1. Create ticket in AWAITING_USER_REPLY, paused_from=READY.
    t = _make_ticket(
        service,
        settings,
        title="ask-user-round-trip",
        updated_at_delta=timedelta(days=4),
    )
    _add_ask_user_thread(service, t.id)
    assert t.paused_from == State.READY.value
    assert t.state is State.AWAITING_USER_REPLY

    # 2. Timeout escalation → BLOCKED.
    result = run_timeout_escalation(settings)
    assert result["escaped"] == 1
    t = service.get(t.id)
    assert t.state is State.BLOCKED
    assert t.blocked_from == State.AWAITING_USER_REPLY.value
    # paused_from must survive the BLOCKED transition.
    assert t.paused_from == State.READY.value

    # 3. Operator answers the question.
    reply = service.answer_pending_question(
        t.id,
        "Here is the clarification you needed.",
        author="operator",
    )
    assert reply is not None

    # 4. Ticket resumes to paused_from (READY).
    t = service.get(t.id)
    assert t.state is State.READY
    assert t.blocked_from is None
    assert t.paused_from is None

    # Verify the [ASK_USER] thread is closed.
    comments = service.list_comments(t.id)
    ask_threads = [
        c for c in comments
        if c.parent_id is None and (c.body or "").startswith("[ASK_USER]")
    ]
    assert len(ask_threads) >= 1
    assert all(t.closed_at is not None for t in ask_threads)


def test_answer_blocked_ask_user_via_route_guard(settings, service):
    """The answer route rejects tickets not in an answerable state."""
    from robotsix_mill.core.service._comments import _CommentMixin

    # A READY ticket is not answerable.
    t = service.create("not-paused", source=SourceKind.AGENT)
    with db.session(settings, service.board_id) as s:
        row = s.get(Ticket, t.id)
        row.state = State.READY
        s.add(row)
        s.commit()
    t = service.get(t.id)

    with pytest.raises(ValueError, match="not awaiting a user reply"):
        service.answer_pending_question(
            t.id,
            "answer",
            author="operator",
        )

    # A BLOCKED ticket that wasn't blocked from AWAITING_USER_REPLY
    # is also not answerable.
    t2 = service.create("blocked-not-ask", source=SourceKind.AGENT)
    with db.session(settings, service.board_id) as s:
        row = s.get(Ticket, t2.id)
        row.state = State.BLOCKED
        row.blocked_from = State.READY.value  # blocked from implement, not ask_user
        s.add(row)
        s.commit()
    t2 = service.get(t2.id)

    with pytest.raises(ValueError, match="not awaiting a user reply"):
        service.answer_pending_question(
            t2.id,
            "answer",
            author="operator",
        )


def test_blocked_to_closed_legal_for_ask_user_timeout(settings, service, monkeypatch):
    """BLOCKED (from ask_user timeout) → CLOSED is a legal terminal edge."""
    from robotsix_mill.agents.runners.timeout_escalation_runner import (
        run_timeout_escalation,
    )

    notifications = []

    def fake_notify(ticket, dst, note, s):
        notifications.append((ticket.id, dst, note))

    monkeypatch.setattr(
        "robotsix_mill.agents.runners.timeout_escalation_runner.send_notification",
        fake_notify,
    )

    settings.timeout_escalation_threshold_seconds = 86400

    # Escalate a stale ask_user ticket to BLOCKED.
    t = _make_ticket(
        service,
        settings,
        title="close-me",
        updated_at_delta=timedelta(days=4),
    )
    _add_ask_user_thread(service, t.id)
    result = run_timeout_escalation(settings)
    assert result["escaped"] == 1
    t = service.get(t.id)
    assert t.state is State.BLOCKED

    # Transition to CLOSED — must succeed (open threads auto-closed).
    updated = service.transition(t.id, State.CLOSED, note="operator closed")
    assert updated.state is State.CLOSED

    # Verify the [ASK_USER] thread was auto-closed.
    comments = service.list_comments(t.id)
    ask_threads = [
        c for c in comments
        if c.parent_id is None and (c.body or "").startswith("[ASK_USER]")
    ]
    assert len(ask_threads) >= 1
    assert all(th.closed_at is not None for th in ask_threads)
