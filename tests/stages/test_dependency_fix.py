"""Direct unit tests for :func:`spawn_dependency_fix`.

The service layer is mocked with a small fake so the spawn-or-reuse,
bidirectional-wiring, and best-effort history-note logic can be
exercised without a real database.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.stages.base import StageContext
from robotsix_mill.stages.dependency_fix import spawn_dependency_fix


class FakeService:
    """Minimal stand-in for ``TicketService`` recording the calls made."""

    def __init__(
        self,
        *,
        proposals: list[SimpleNamespace] | None = None,
        created_id: str = "fix-1",
        note_failures: set[str] | None = None,
    ) -> None:
        self.proposals = proposals or []
        self.created_id = created_id
        self.note_failures = note_failures or set()
        self.create_calls: list[dict] = []
        self.depends_on_calls: list[tuple[str, list[str]]] = []
        self.unblocks_calls: list[tuple[str, list[str]]] = []
        self.history_notes: list[tuple[str, str]] = []

    def recent_proposals_for(self, source, limit=100):
        return self.proposals

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(id=self.created_id)

    def set_depends_on(self, ticket_id, deps):
        self.depends_on_calls.append((ticket_id, deps))

    def set_unblocks(self, ticket_id, targets):
        self.unblocks_calls.append((ticket_id, targets))

    def add_history_note(self, ticket_id, note):
        if ticket_id in self.note_failures:
            raise RuntimeError("boom")
        self.history_notes.append((ticket_id, note))


def _ctx(service: FakeService, *, board_id: str | None = "test-board") -> StageContext:
    repo_config = SimpleNamespace(board_id=board_id) if board_id is not None else None
    return StageContext(settings=None, service=service, repo_config=repo_config)


def _spawn(service: FakeService, **overrides):
    ticket = SimpleNamespace(id="orig-1")
    kwargs = dict(
        title="fix the thing",
        description="please fix it",
        source_kind=SourceKind.CI_FIX_DEPENDENCY,
        block_reason_prefix="out of scope",
    )
    kwargs.update(overrides)
    return spawn_dependency_fix(ticket, _ctx(service), **kwargs)


def test_fresh_spawn_creates_new_ticket() -> None:
    service = FakeService(created_id="fix-99")
    outcome = _spawn(service)

    assert len(service.create_calls) == 1
    call = service.create_calls[0]
    assert call["title"] == "fix the thing"
    assert call["description"] == "please fix it"
    assert call["source"] == SourceKind.CI_FIX_DEPENDENCY
    assert call["kind"] == "task"
    assert call["board_id"] == "test-board"
    assert call["priority"] is False
    assert "fix-99" in (outcome.note or "")


def test_priority_flag_forwarded() -> None:
    service = FakeService()
    _spawn(service, priority=True)
    assert service.create_calls[0]["priority"] is True


def test_dedup_reuses_open_ticket_with_same_title() -> None:
    existing = SimpleNamespace(
        id="fix-existing", title="fix the thing", state=State.READY
    )
    service = FakeService(proposals=[existing])

    outcome = _spawn(service)

    # No new ticket created; the existing one is reused.
    assert service.create_calls == []
    assert service.depends_on_calls == [("orig-1", ["fix-existing"])]
    assert "fix-existing" in (outcome.note or "")


def test_dedup_ignores_closed_or_done_tickets() -> None:
    closed = SimpleNamespace(id="fix-closed", title="fix the thing", state=State.CLOSED)
    done = SimpleNamespace(id="fix-done", title="fix the thing", state=State.DONE)
    service = FakeService(proposals=[closed, done], created_id="fix-new")

    _spawn(service)

    # A fresh ticket is created because matching candidates are terminal.
    assert len(service.create_calls) == 1


def test_dedup_ignores_different_title() -> None:
    other = SimpleNamespace(id="fix-other", title="something else", state=State.READY)
    service = FakeService(proposals=[other], created_id="fix-new")

    _spawn(service)

    assert len(service.create_calls) == 1


def test_bidirectional_wiring() -> None:
    service = FakeService(created_id="fix-7")
    _spawn(service)

    assert service.depends_on_calls == [("orig-1", ["fix-7"])]
    assert service.unblocks_calls == [("fix-7", ["orig-1"])]


def test_history_notes_recorded_on_both_tickets() -> None:
    service = FakeService(created_id="fix-7")
    _spawn(service)

    notes = dict(service.history_notes)
    assert "orig-1" in notes
    assert "fix-7" in notes
    assert "out of scope" in notes["orig-1"]
    assert "orig-1" in notes["fix-7"]


def test_history_note_failure_is_caught_and_logged(caplog) -> None:
    # Both history-note writes raise; wiring and outcome must still succeed.
    service = FakeService(created_id="fix-7", note_failures={"orig-1", "fix-7"})

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.dependency_fix"):
        outcome = _spawn(service)

    assert service.history_notes == []
    assert service.depends_on_calls == [("orig-1", ["fix-7"])]
    assert service.unblocks_calls == [("fix-7", ["orig-1"])]
    assert outcome.next_state == State.BLOCKED
    assert len(caplog.records) == 2


def test_return_value_is_blocked_with_resume_note() -> None:
    service = FakeService(created_id="fix-7")
    outcome = _spawn(service)

    assert outcome.next_state == State.BLOCKED
    assert "fix-7" in (outcome.note or "")
    assert "Auto-resumes" in (outcome.note or "")
    assert "out of scope" in (outcome.note or "")


def test_board_id_none_when_no_repo_config() -> None:
    service = FakeService()
    ticket = SimpleNamespace(id="orig-1")
    outcome = spawn_dependency_fix(
        ticket,
        _ctx(service, board_id=None),
        title="t",
        description="d",
        source_kind=SourceKind.CI_FIX_DEPENDENCY,
        block_reason_prefix="prefix",
    )
    assert service.create_calls[0]["board_id"] is None
    assert outcome.next_state == State.BLOCKED
