"""Direct unit tests for :func:`spawn_dependency_fix`.

The service layer is mocked with a small fake so the spawn-or-reuse,
bidirectional-wiring, and best-effort history-note logic can be
exercised without a real database.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from robotsix_mill.core.models import SourceKind, TicketKind
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
        recent_tickets_data: list[SimpleNamespace] | None = None,
        existing_labels: list[str] | None = None,
        existing_unblocks: list[str] | None = None,
    ) -> None:
        self.proposals = proposals or []
        self.created_id = created_id
        self.note_failures = note_failures or set()
        self.create_calls: list[dict] = []
        self.depends_on_calls: list[tuple[str, list[str]]] = []
        self.unblocks_calls: list[tuple[str, list[str]]] = []
        self.history_notes: list[tuple[str, str]] = []
        self.set_labels_calls: list[tuple[str, list[str]]] = []
        self.transition_calls: list[tuple[str, object, str | None]] = []
        self.recent_tickets_data = recent_tickets_data or []
        self._existing_labels = existing_labels
        self._existing_unblocks = existing_unblocks

    def recent_proposals_for(self, source, limit=100):
        return self.proposals

    def recent_tickets(self, limit=100, *, sources=None, board_id=None):
        return self.recent_tickets_data

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return SimpleNamespace(id=self.created_id)

    def get(self, ticket_id):
        labels = (
            json.dumps(self._existing_labels)
            if self._existing_labels is not None
            else None
        )
        unblocks = (
            json.dumps(self._existing_unblocks)
            if self._existing_unblocks is not None
            else None
        )
        return SimpleNamespace(id=ticket_id, labels=labels, unblocks=unblocks)

    def set_labels(self, ticket_id, labels):
        self.set_labels_calls.append((ticket_id, labels))

    def set_depends_on(self, ticket_id, deps):
        self.depends_on_calls.append((ticket_id, deps))

    def set_unblocks(self, ticket_id, targets):
        self.unblocks_calls.append((ticket_id, targets))

    def transition(self, ticket_id, dst, note=None):
        self.transition_calls.append((ticket_id, dst, note))
        return SimpleNamespace(id=ticket_id, state=dst)

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
    assert call["kind"] == TicketKind.TASK
    assert call["board_id"] == "test-board"
    assert call["priority"] is False
    assert "fix-99" in (outcome.note or "")


def test_fresh_spawn_transitions_to_ready() -> None:
    """A freshly created dependency fix ticket is transitioned to READY
    so the worker's poll loop picks it up — preventing the silent
    deadlock where a draft fix ticket is created but never enqueued."""
    service = FakeService(created_id="fix-99")
    _spawn(service)

    assert len(service.transition_calls) == 1
    tid, dst, note = service.transition_calls[0]
    assert tid == "fix-99"
    assert dst == State.READY
    assert note is not None
    assert "dependency fix" in note


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


def test_dedup_reuse_does_not_transition() -> None:
    """When an existing ticket is reused via title dedup, no transition
    is attempted — the existing ticket may already be in a valid state."""
    existing = SimpleNamespace(
        id="fix-existing", title="fix the thing", state=State.READY
    )
    service = FakeService(proposals=[existing])

    _spawn(service)

    assert service.transition_calls == []


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
    records = [
        r for r in caplog.records if r.name == "robotsix_mill.stages.dependency_fix"
    ]
    assert len(records) == 2


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


# ---------------------------------------------------------------------------
# Label-based dedup (fingerprint)
# ---------------------------------------------------------------------------


def test_label_dedup_reuses_open_ticket_with_matching_label() -> None:
    """When dedup_labels contains a label that matches an existing open
    ticket, that ticket is reused and no new ticket is created."""
    existing = SimpleNamespace(
        id="fix-existing",
        title="different title",  # title doesn't match
        state=State.READY,
        labels=json.dumps(["ci_fp:abc123", "bug"]),
    )
    service = FakeService(recent_tickets_data=[existing])

    outcome = _spawn(service, dedup_labels=["ci_fp:abc123"])

    # No new ticket created; the existing one is reused via label match.
    assert service.create_calls == []
    assert service.depends_on_calls == [("orig-1", ["fix-existing"])]
    assert "fix-existing" in (outcome.note or "")


def test_label_dedup_ignores_terminal_tickets() -> None:
    """CLOSED, DONE, and ERRORED tickets are skipped by label dedup,
    causing a fresh create."""
    closed = SimpleNamespace(
        id="fix-closed", title="t", state=State.CLOSED, labels=json.dumps(["ci_fp:abc"])
    )
    done = SimpleNamespace(
        id="fix-done", title="t", state=State.DONE, labels=json.dumps(["ci_fp:abc"])
    )
    errored = SimpleNamespace(
        id="fix-errored",
        title="t",
        state=State.ERRORED,
        labels=json.dumps(["ci_fp:abc"]),
    )
    service = FakeService(
        recent_tickets_data=[closed, done, errored],
        created_id="fix-new",
    )

    outcome = _spawn(service, dedup_labels=["ci_fp:abc"])

    # All candidates are terminal → fresh create.
    assert len(service.create_calls) == 1
    assert "fix-new" in (outcome.note or "")


def test_label_dedup_no_match_creates_new() -> None:
    """When no existing ticket has any of the dedup labels, a fresh ticket
    is created."""
    other = SimpleNamespace(
        id="fix-other",
        title="t",
        state=State.READY,
        labels=json.dumps(["ci_fp:xyz"]),
    )
    service = FakeService(
        recent_tickets_data=[other],
        created_id="fix-new",
    )

    outcome = _spawn(service, dedup_labels=["ci_fp:abc"])

    assert len(service.create_calls) == 1
    assert "fix-new" in (outcome.note or "")


def test_label_stored_on_new_ticket() -> None:
    """When a fresh ticket is created with dedup_labels, the labels are
    stored on the new ticket via set_labels."""
    service = FakeService(created_id="fix-7")

    _spawn(service, dedup_labels=["ci_fp:abc123", "ci_fp:def456"])

    assert len(service.set_labels_calls) == 1
    tid, labels = service.set_labels_calls[0]
    assert tid == "fix-7"
    assert "ci_fp:abc123" in labels
    assert "ci_fp:def456" in labels


def test_label_stored_preserves_existing_labels() -> None:
    """When the newly created ticket already has labels (e.g. from create),
    dedup_labels are appended, not overwritten."""
    service = FakeService(
        created_id="fix-7",
        existing_labels=["priority"],
    )

    _spawn(service, dedup_labels=["ci_fp:abc"])

    assert len(service.set_labels_calls) == 1
    _tid, labels = service.set_labels_calls[0]
    assert labels == ["priority", "ci_fp:abc"]


def test_label_dedup_skipped_when_dedup_labels_none() -> None:
    """When dedup_labels is None (caller doesn't pass it), the function
    behaves exactly as before — title-based dedup only, no recent_tickets
    call, no set_labels call."""
    existing = SimpleNamespace(
        id="fix-existing", title="fix the thing", state=State.READY
    )
    service = FakeService(proposals=[existing])

    _spawn(service)  # no dedup_labels

    assert service.create_calls == []
    assert service.set_labels_calls == []
    assert service.depends_on_calls == [("orig-1", ["fix-existing"])]


def test_label_dedup_skipped_when_board_id_none() -> None:
    """When there is no board_id (repo_config is None), label dedup is
    skipped and title-based dedup runs instead."""
    service = FakeService(created_id="fix-new")
    ticket = SimpleNamespace(id="orig-1")
    spawn_dependency_fix(
        ticket,
        _ctx(service, board_id=None),
        title="t",
        description="d",
        source_kind=SourceKind.CI_FIX_DEPENDENCY,
        block_reason_prefix="prefix",
        dedup_labels=["ci_fp:abc"],
    )
    # No board_id → label dedup skipped → fresh create.
    assert len(service.create_calls) == 1
    # Labels still stored on the new ticket.
    assert len(service.set_labels_calls) == 1


def test_label_dedup_falls_back_to_title_when_no_label_match() -> None:
    """When dedup_labels are provided but no label match is found, the
    existing title-based dedup still runs as a fallback."""
    # recent_tickets has no label match, but proposals has a title match.
    label_mismatch = SimpleNamespace(
        id="fix-label",
        title="different",
        state=State.READY,
        labels=json.dumps(["ci_fp:xyz"]),
    )
    title_match = SimpleNamespace(
        id="fix-title",
        title="fix the thing",
        state=State.READY,
    )
    service = FakeService(
        recent_tickets_data=[label_mismatch],
        proposals=[title_match],
    )

    _spawn(service, dedup_labels=["ci_fp:abc"])

    # Title-based dedup reuses the existing title match.
    assert service.create_calls == []
    assert service.depends_on_calls == [("orig-1", ["fix-title"])]


# ---------------------------------------------------------------------------
# unblocks merge — multiple tickets parked on the same fix
# ---------------------------------------------------------------------------


def test_unblocks_merge_across_multiple_parkings() -> None:
    """When a second ticket is parked on the same fix ticket (via dedup),
    its id is appended to the existing unblocks list — not replacing it."""
    # The fix ticket already has "parked-1" in its unblocks.
    service = FakeService(
        created_id="fix-shared",
        existing_unblocks=["parked-1"],
    )

    ticket = SimpleNamespace(id="parked-2")
    spawn_dependency_fix(
        ticket,
        _ctx(service),
        title="fix shared",
        description="shared fix",
        source_kind=SourceKind.CI_FIX_DEPENDENCY,
        block_reason_prefix="shared failure",
    )

    # unblocks should contain both tickets.
    assert len(service.unblocks_calls) == 1
    _tid, targets = service.unblocks_calls[0]
    assert _tid == "fix-shared"
    assert "parked-1" in targets
    assert "parked-2" in targets
    assert len(targets) == 2


def test_unblocks_merge_handles_malformed_existing() -> None:
    """Corrupted (non-JSON, non-list) existing unblocks are treated as
    empty and the new ticket id is still written."""
    service = FakeService(
        created_id="fix-shared",
        existing_unblocks="not-json",  # type: ignore[arg-type] — test input
    )

    ticket = SimpleNamespace(id="parked-1")
    spawn_dependency_fix(
        ticket,
        _ctx(service),
        title="fix shared",
        description="shared fix",
        source_kind=SourceKind.CI_FIX_DEPENDENCY,
        block_reason_prefix="shared failure",
    )

    # Should have recovered and written just the new ticket.
    assert len(service.unblocks_calls) == 1
    _tid, targets = service.unblocks_calls[0]
    assert targets == ["parked-1"]


def test_unblocks_merge_skips_self_reference() -> None:
    """If the existing unblocks list somehow contains the fix ticket's own
    id, it is filtered out before merging."""
    service = FakeService(
        created_id="fix-shared",
        existing_unblocks=["fix-shared", "parked-1"],
    )

    ticket = SimpleNamespace(id="parked-2")
    spawn_dependency_fix(
        ticket,
        _ctx(service),
        title="fix shared",
        description="shared fix",
        source_kind=SourceKind.CI_FIX_DEPENDENCY,
        block_reason_prefix="shared failure",
    )

    _tid, targets = service.unblocks_calls[0]
    assert "fix-shared" not in targets
    assert targets == ["parked-1", "parked-2"]
