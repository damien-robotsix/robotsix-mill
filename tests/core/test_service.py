import pytest

from robotsix_mill.core.service import TransitionError
from robotsix_mill.core.states import State, can_transition


def test_create_writes_db_and_workspace(service):
    t = service.create("Add a widget", "do the thing")
    assert t.state is State.DRAFT
    ws = service.workspace(t)
    assert ws.read_description() == "do the thing"
    assert t.content_hash == ws.content_hash()
    assert service.get(t.id).title == "Add a widget"
    assert service.history(t.id)[0].note == "created"


def test_default_source_is_user(service):
    t = service.create("Default source test")
    assert t.source == "user"


def test_explicit_source_is_stored(service):
    t = service.create("Explicit source", source="retrospect")
    assert t.source == "retrospect"


def test_list_filters_by_state(service):
    a = service.create("a")
    service.create("b")
    service.transition(a.id, State.READY)
    assert [t.id for t in service.list(state=State.READY)] == [a.id]
    assert len(service.list(state=State.DRAFT)) == 1
    assert len(service.list()) == 2


def test_transition_records_history(service):
    t = service.create("x")
    service.transition(t.id, State.READY, note="refined")
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    hist = service.history(t.id)
    assert hist[-1].state is State.READY
    assert hist[-1].note == "refined"


def test_illegal_transition_rejected(service):
    t = service.create("x")
    with pytest.raises(TransitionError):
        # draft -> human_mr_approval is not allowed (drafts can't jump onto a PR).
        # draft -> done IS allowed (refine's dedup-discard path uses it).
        service.transition(t.id, State.HUMAN_MR_APPROVAL)


def test_state_machine_edges():
    # draft → ready → deliverable → human_mr_approval(PR) → done(merged) → reviewed
    assert can_transition(State.DRAFT, State.READY)
    assert can_transition(State.READY, State.DELIVERABLE)
    assert can_transition(State.DELIVERABLE, State.HUMAN_MR_APPROVAL)
    assert can_transition(State.HUMAN_MR_APPROVAL, State.DONE)      # merged
    assert can_transition(State.HUMAN_MR_APPROVAL, State.BLOCKED)   # closed unmerged
    assert can_transition(State.HUMAN_MR_APPROVAL, State.REBASING)  # conflicting
    assert can_transition(State.REBASING, State.HUMAN_MR_APPROVAL)  # rebase success
    assert can_transition(State.REBASING, State.BLOCKED)    # rebase exhausted
    assert can_transition(State.REBASING, State.ERRORED)    # rebase crash
    assert can_transition(State.DONE, State.CLOSED)       # retrospected
    assert not can_transition(State.CLOSED, State.DONE)   # terminal
    assert not can_transition(State.DELIVERABLE, State.DONE)  # via human_mr_approval
    assert not can_transition(State.READY, State.DONE)


# --- BLOCKED resume path ---


def test_blocked_from_done_can_transition_with_blocked_from():
    """can_transition(BLOCKED, DONE) returns True when blocked_from=DONE."""
    assert can_transition(State.BLOCKED, State.DONE, blocked_from=State.DONE)


def test_blocked_from_done_fails_without_blocked_from():
    """can_transition(BLOCKED, DONE) returns False without blocked_from."""
    assert not can_transition(State.BLOCKED, State.DONE)


def test_blocked_to_ready_always_allowed():
    """Existing BLOCKED → READY works regardless of blocked_from."""
    assert can_transition(State.BLOCKED, State.READY)
    assert can_transition(State.BLOCKED, State.READY, blocked_from=State.DONE)
    assert can_transition(State.BLOCKED, State.READY, blocked_from=State.READY)
    assert can_transition(State.BLOCKED, State.READY, blocked_from=None)


def test_blocked_to_draft_always_allowed():
    """Existing BLOCKED → DRAFT works regardless of blocked_from."""
    assert can_transition(State.BLOCKED, State.DRAFT)
    assert can_transition(State.BLOCKED, State.DRAFT, blocked_from=State.DONE)
    assert can_transition(State.BLOCKED, State.DRAFT, blocked_from=None)


def test_blocked_from_implement_can_resume_to_ready():
    """BLOCKED from READY can resume back to READY."""
    assert can_transition(State.BLOCKED, State.READY, blocked_from=State.READY)


def test_blocked_from_refine_can_resume_to_draft():
    """BLOCKED from DRAFT can resume back to DRAFT (also covered by override)."""
    assert can_transition(State.BLOCKED, State.DRAFT, blocked_from=State.DRAFT)


def test_blocked_resume_wrong_state_rejected():
    """BLOCKED from DONE cannot resume to a non-matching state via resume-only path."""
    assert not can_transition(
        State.BLOCKED, State.DELIVERABLE, blocked_from=State.DONE
    )


# --- REBASING-specific can_transition tests ---

def test_can_transition_covers_rebasing():
    """Verify all new edges involving REBASING."""
    # HUMAN_MR_APPROVAL → REBASING
    assert can_transition(State.HUMAN_MR_APPROVAL, State.REBASING)
    # REBASING → HUMAN_MR_APPROVAL
    assert can_transition(State.REBASING, State.HUMAN_MR_APPROVAL)
    # REBASING → ERRORED
    assert can_transition(State.REBASING, State.ERRORED)
    # REBASING → BLOCKED
    assert can_transition(State.REBASING, State.BLOCKED)
    # BLOCKED → REBASING with blocked_from=REBASING (resume)
    assert can_transition(
        State.BLOCKED, State.REBASING, blocked_from=State.REBASING
    )
    # BLOCKED → REBASING without blocked_from → False
    assert not can_transition(State.BLOCKED, State.REBASING)
    # REBASING → DONE is NOT allowed (must go through HUMAN_MR_APPROVAL)
    assert not can_transition(State.REBASING, State.DONE)


# --- Service-level integration tests ---


def test_transition_to_blocked_records_blocked_from(service):
    """Transitioning to BLOCKED sets blocked_from to the current state."""
    t = service.create("block test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    assert reloaded.blocked_from == State.READY.value


def test_resume_blocked_back_to_originating_state(service):
    """resume_blocked transitions BLOCKED → <blocked_from>."""
    t = service.create("resume test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    resumed = service.resume_blocked(t.id)
    assert resumed.state is State.READY
    assert resumed.blocked_from is None
    hist = service.history(t.id)
    assert hist[-1].state is State.READY
    assert "resumed from blocked" in (hist[-1].note or "")


def test_resume_blocked_after_retrospect_failure(service):
    """Full scenario: DONE → BLOCKED → resume → DONE → CLOSED.
    This simulates a retrospect failure and proves the ticket can be
    recovered without re-running implement or refine."""
    t = service.create("retrospect fail test")
    # Walk through the pipeline to DONE
    service.transition(t.id, State.READY, note="refined")
    service.transition(t.id, State.DELIVERABLE, note="implemented")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="PR opened")
    service.transition(t.id, State.DONE, note="merged")
    # Now retrospect fails → BLOCKED
    service.transition(t.id, State.BLOCKED, note="retrospect failed")
    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    assert reloaded.blocked_from == State.DONE.value

    # Resume back to DONE
    resumed = service.resume_blocked(t.id)
    assert resumed.state is State.DONE
    assert resumed.blocked_from is None

    # Re-run retrospect → CLOSED
    service.transition(t.id, State.CLOSED, note="retrospect succeeded")
    closed = service.get(t.id)
    assert closed.state is State.CLOSED


def test_blocked_to_ready_still_works_after_blocked_from_recorded(service):
    """The existing BLOCKED → READY override still works."""
    t = service.create("override test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    assert service.get(t.id).blocked_from == State.READY.value
    # Override to READY
    service.transition(t.id, State.READY, note="manual unblock")
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    assert reloaded.blocked_from is None


def test_blocked_to_draft_still_works_after_blocked_from_recorded(service):
    """The existing BLOCKED → DRAFT override still works."""
    t = service.create("override draft test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    # Override to DRAFT
    service.transition(t.id, State.DRAFT, note="manual unblock to draft")
    reloaded = service.get(t.id)
    assert reloaded.state is State.DRAFT
    assert reloaded.blocked_from is None


def test_resume_blocked_rejects_non_blocked_ticket(service):
    """resume_blocked raises TransitionError if ticket is not BLOCKED."""
    t = service.create("not blocked")
    with pytest.raises(TransitionError, match="not BLOCKED"):
        service.resume_blocked(t.id)


def test_resume_blocked_rejects_missing_blocked_from(service):
    """resume_blocked raises TransitionError if blocked_from is not set."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket

    t = service.create("no blocked_from")
    # Manually set the ticket to BLOCKED with blocked_from=None via
    # direct DB manipulation to simulate a legacy record.
    with db.session(service.settings) as s:
        ticket = s.get(Ticket, t.id)
        ticket.state = State.BLOCKED
        ticket.blocked_from = None
        s.add(ticket)
        s.commit()

    with pytest.raises(TransitionError, match="no blocked_from"):
        service.resume_blocked(t.id)


def test_transition_table_consistency():
    """Every source state's declared destinations should be reachable
    and no dangling states exist."""
    from robotsix_mill.core.states import TRANSITIONS

    all_states = set(State)
    declared_sources = set(TRANSITIONS.keys())
    assert declared_sources == all_states, "TRANSITIONS must cover every State"

    for src, dsts in TRANSITIONS.items():
        for dst in dsts:
            assert dst in all_states, f"{src} -> {dst}: {dst} not a State"
            # Verify can_transition returns True for these edges
            assert can_transition(src, dst), (
                f"can_transition({src}, {dst}) should be True per TRANSITIONS"
            )

    # Terminal states: CLOSED must have no outgoing edges
    assert TRANSITIONS[State.CLOSED] == set()

    # Every active state must be able to reach BLOCKED and ERRORED
    for src in [
        State.DRAFT, State.HUMAN_ISSUE_APPROVAL, State.READY,
        State.DELIVERABLE, State.HUMAN_MR_APPROVAL, State.REBASING, State.DONE,
    ]:
        assert State.BLOCKED in TRANSITIONS[src], (
            f"{src} missing BLOCKED escalation edge"
        )
        assert State.ERRORED in TRANSITIONS[src], (
            f"{src} missing ERRORED escalation edge"
        )


# --- cost_usd (Langfuse-synced, absolute) -----------------------------

def test_initial_cost_is_zero(service):
    t = service.create("cost test")
    assert t.cost_usd == 0.0


def test_origin_session_stored_when_provided(service):
    t = service.create("origin test", origin_session="audit-20250101-abc123")
    assert t.origin_session == "audit-20250101-abc123"
    # Verify it's persisted in the DB.
    reloaded = service.get(t.id)
    assert reloaded.origin_session == "audit-20250101-abc123"


def test_origin_session_is_none_by_default(service):
    t = service.create("no origin")
    assert t.origin_session is None
    reloaded = service.get(t.id)
    assert reloaded.origin_session is None



def test_delete_removes_row_events_and_workspace(service, settings):
    t = service.create("junk: no notable issues clean run", "noise")
    service.transition(t.id, State.READY)  # creates a TicketEvent too
    ws_dir = settings.workspaces_dir / t.id
    assert ws_dir.exists()

    # Pre-create a dummy conversation file to verify it's cleaned up.
    conv_dir = settings.data_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    conv_file = conv_dir / f"{t.id}.json"
    conv_file.write_text('[]', encoding="utf-8")
    assert conv_file.exists()

    assert service.get(t.id) is not None
    assert service.history(t.id)  # has events

    assert service.delete(t.id) is True
    assert service.get(t.id) is None
    assert service.history(t.id) == []   # events gone
    assert not ws_dir.exists()           # workspace dir gone
    assert not conv_file.exists()        # conversation file gone


def test_delete_missing_ticket_returns_false(service):
    assert service.delete("does-not-exist") is False


# --- depends_on --------------------------------------------------------

def test_create_stores_depends_on(service):
    t = service.create("Dep test", depends_on='["abc123", "def456"]')
    assert t.depends_on == '["abc123", "def456"]'
    reloaded = service.get(t.id)
    assert reloaded.depends_on == '["abc123", "def456"]'


def test_create_without_depends_on_has_none(service):
    t = service.create("No dep")
    assert t.depends_on is None


def test_self_dependency_rejected_deterministic(service, monkeypatch):
    """Create a ticket whose depends_on includes its own (deterministic) ID."""
    import datetime as dt

    # Freeze the timestamp and token to get a predictable ID.
    fake_now = dt.datetime(2025, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(
        "robotsix_mill.core.service.datetime",
        type("m", (), {
            "now": classmethod(lambda cls, tz=None: fake_now),
            "timezone": dt.timezone,
        })(),
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.token_hex",
        lambda n: "abcd1234",
    )
    # The ID will be: 20250101T000000Z-self-dep-test-abcd1234
    expected_id = "20250101T000000Z-self-dep-test-abcd1234"
    with pytest.raises(ValueError, match="cannot depend on itself"):
        service.create("Self-dep test", depends_on=f'["{expected_id}"]')


def test_parse_depends_on_returns_list(service):
    t = service.create("Parse test", depends_on='["a","b"]')
    result = service._parse_depends_on(t)
    assert result == ["a", "b"]


def test_parse_depends_on_none_returns_empty(service):
    t = service.create("No dep parse")
    result = service._parse_depends_on(t)
    assert result == []


def test_parse_depends_on_empty_string_returns_empty(service):
    t = service.create("Empty dep parse", depends_on="")
    result = service._parse_depends_on(t)
    assert result == []


def test_unmet_dependencies_all_satisfied(service):
    """When all deps are CLOSED, unmet_dependencies returns empty."""
    dep = service.create("Dep ticket")
    service.transition(dep.id, State.READY)
    service.transition(dep.id, State.DELIVERABLE)
    service.transition(dep.id, State.HUMAN_MR_APPROVAL)
    service.transition(dep.id, State.DONE)
    service.transition(dep.id, State.CLOSED)

    t = service.create("Depender", depends_on=f'["{dep.id}"]')
    assert service.unmet_dependencies(t) == []


def test_unmet_dependencies_some_unmet(service):
    """When some deps are not CLOSED/DONE, they appear in unmet."""
    dep_a = service.create("Dep A")
    dep_b = service.create("Dep B")
    # Close dep_a, leave dep_b in DRAFT
    service.transition(dep_a.id, State.READY)
    service.transition(dep_a.id, State.DELIVERABLE)
    service.transition(dep_a.id, State.HUMAN_MR_APPROVAL)
    service.transition(dep_a.id, State.DONE)
    service.transition(dep_a.id, State.CLOSED)

    t = service.create("Depender", depends_on=f'["{dep_a.id}", "{dep_b.id}"]')
    unmet = service.unmet_dependencies(t)
    assert unmet == [dep_b.id]


def test_unmet_dependencies_missing_dep_satisfied(service, caplog):
    """A nonexistent dep ID is treated as satisfied with a debug log."""
    t = service.create("Depender", depends_on='["nonexistent-id"]')
    unmet = service.unmet_dependencies(t)
    assert unmet == []
    # The warning should be logged at debug level
    # (caplog captures at WARNING by default, but we log at debug)


def test_unmet_dependencies_direct_cycle_satisfied(service, caplog):
    """A → B, B → A: unmet_dependencies(A) returns empty."""
    a = service.create("Ticket A")
    b = service.create("Ticket B")
    # Manually set mutual deps via DB (no update API)
    from robotsix_mill.core import db as core_db
    from robotsix_mill.core.models import Ticket as TicketModel
    with core_db.session(service.settings) as s:
        ta = s.get(TicketModel, a.id)
        tb = s.get(TicketModel, b.id)
        ta.depends_on = f'["{b.id}"]'
        tb.depends_on = f'["{a.id}"]'
        s.add(ta)
        s.add(tb)
        s.commit()

    # Re-read both
    a = service.get(a.id)
    b = service.get(b.id)

    # Both should see no unmet deps (cycle treated as satisfied)
    assert service.unmet_dependencies(a) == []
    assert service.unmet_dependencies(b) == []


def test_unmet_dependencies_no_deps_returns_empty(service):
    t = service.create("No deps at all")
    assert service.unmet_dependencies(t) == []


def test_migration_idempotent(settings):
    """Calling _run_migrations twice should not crash."""
    from robotsix_mill.core.db import _run_migrations
    _run_migrations(settings)
    _run_migrations(settings)  # second call must not raise


# ---------------------------------------------------------------------------
# Epic tests
# ---------------------------------------------------------------------------

def test_create_epic(service):
    """Creating with kind='epic' sets state to EPIC_OPEN."""
    t = service.create("My Epic", "Big picture", kind="epic")
    assert t.state == State.EPIC_OPEN
    assert t.kind == "epic"


def test_create_child_with_parent_id(service):
    """Creating a child with parent_id links it to the epic."""
    epic = service.create("Epic", "Overview", kind="epic")
    child = service.create("Child", "Detail", kind="task", parent_id=epic.id)
    assert child.parent_id == epic.id
    # Verify persisted
    reloaded = service.get(child.id)
    assert reloaded.parent_id == epic.id


def test_create_child_nonexistent_parent(service):
    """parent_id pointing to a missing ticket raises ValueError."""
    with pytest.raises(ValueError, match="does not exist"):
        service.create("Orphan", "desc", parent_id="nonexistent-id")


def test_get_epic_context_returns_description(service):
    """get_epic_context returns the parent epic description wrapped in tags."""
    epic = service.create("Epic", "Big picture description", kind="epic")
    child = service.create("Child", "detail", kind="task", parent_id=epic.id)
    ctx = service.get_epic_context(child)
    assert ctx == "<epic_context>\nBig picture description\n</epic_context>"


def test_get_epic_context_no_parent(service):
    """get_epic_context returns '' for a ticket without a parent."""
    t = service.create("Standalone")
    assert service.get_epic_context(t) == ""


def test_get_epic_context_parent_not_epic(service):
    """get_epic_context returns '' when parent is not an epic."""
    parent = service.create("Regular parent", kind="task")
    child = service.create("Child", "desc", kind="task", parent_id=parent.id)
    assert service.get_epic_context(child) == ""


def test_list_children(service):
    """list_children returns all tickets with the given parent_id."""
    epic = service.create("Epic", "Overview", kind="epic")
    c1 = service.create("Child 1", kind="task", parent_id=epic.id)
    c2 = service.create("Child 2", kind="task", parent_id=epic.id)
    c3 = service.create("Child 3", kind="task", parent_id=epic.id)
    children = service.list_children(epic.id)
    assert len(children) == 3
    child_ids = {c.id for c in children}
    assert child_ids == {c1.id, c2.id, c3.id}


# --- archived-ticket purge ---------------------------------------------


def _close_ticket(service, ticket):
    """Transition a DRAFT task to CLOSED."""
    service.transition(ticket.id, State.DONE)
    service.transition(ticket.id, State.CLOSED)


def _answer_ticket(service, ticket):
    """Transition an ASKED inquiry to ANSWERED."""
    service.transition(ticket.id, State.ANSWERED)


def _close_epic(service, ticket):
    """Transition an EPIC_OPEN epic to EPIC_CLOSED."""
    service.transition(ticket.id, State.EPIC_CLOSED)


def _terminal_count(service):
    """Return the number of terminal-state tickets in the DB."""
    return len(service.list(
        exclude_states=[
            s for s in State
            if s not in {State.CLOSED, State.ANSWERED, State.EPIC_CLOSED}
        ]
    ))


class TestArchivedPurge:
    """Tests for insertion-driven purge of terminal (archived) tickets."""

    def test_no_op_when_under_cap(self, service, settings):
        """No tickets are deleted when the terminal count is under the cap."""
        settings.max_archived_tickets = 10
        for i in range(5):
            t = service.create(f"task {i}")
            _close_ticket(service, t)
        assert _terminal_count(service) == 5

    def test_deletes_oldest_on_cap_exceeded(self, service, settings):
        """When closing ticket N+1 exceeds the cap, the oldest terminal
        ticket is deleted."""
        settings.max_archived_tickets = 3
        tickets = []
        for i in range(4):
            t = service.create(f"task {i}")
            _close_ticket(service, t)
            tickets.append(t)

        # The oldest (tickets[0]) should have been purged.
        assert service.get(tickets[0].id) is None
        # The other three should still exist.
        for t in tickets[1:]:
            assert service.get(t.id) is not None
        assert _terminal_count(service) == 3

    def test_answered_triggers_purge(self, service, settings):
        """Answering an inquiry (ANSWERED) also triggers the purge."""
        settings.max_archived_tickets = 2
        inquiries = []
        for i in range(3):
            t = service.create(f"inquiry {i}", kind="inquiry")
            _answer_ticket(service, t)
            inquiries.append(t)

        # Oldest should be purged.
        assert service.get(inquiries[0].id) is None
        assert service.get(inquiries[1].id) is not None
        assert service.get(inquiries[2].id) is not None
        assert _terminal_count(service) == 2

    def test_epic_closed_triggers_purge(self, service, settings):
        """Closing an epic (EPIC_CLOSED) also triggers the purge."""
        settings.max_archived_tickets = 2
        epics = []
        for i in range(3):
            t = service.create(f"epic {i}", kind="epic")
            _close_epic(service, t)
            epics.append(t)

        assert service.get(epics[0].id) is None
        assert service.get(epics[1].id) is not None
        assert service.get(epics[2].id) is not None
        assert _terminal_count(service) == 2

    def test_skip_parent_of_active_child(self, service, settings):
        """A terminal ticket that is the parent of an active child is
        skipped during purge; the next-oldest eligible ticket is
        deleted instead."""
        settings.max_archived_tickets = 2

        # Create 3 terminal tickets.
        t1 = service.create("oldest task")
        _close_ticket(service, t1)

        t2 = service.create("parent task")
        _close_ticket(service, t2)

        t3 = service.create("youngest task")
        _close_ticket(service, t3)

        # t2 has an active (non-terminal) child.
        child = service.create("active child", parent_id=t2.id)
        assert child.state == State.DRAFT  # active

        # Now trigger purge by closing a 4th ticket.
        t4 = service.create("overflow task")
        _close_ticket(service, t4)

        # t2 (parent of active child) should survive.
        assert service.get(t2.id) is not None
        # t1 (oldest, no active children) should be purged.
        assert service.get(t1.id) is None
        # t3 (next oldest after t1) also has no children, so it is
        # purged to bring the count down to the cap of 2.
        assert service.get(t3.id) is None
        # t4 (just closed) survives.
        assert service.get(t4.id) is not None
        # Terminal count is 2: t2 (skipped parent) + t4.
        assert _terminal_count(service) == 2

    def test_max_archived_zero_disables_purge(self, service, settings):
        """Setting max_archived_tickets = 0 disables purging entirely."""
        settings.max_archived_tickets = 0
        for i in range(50):
            t = service.create(f"task {i}")
            _close_ticket(service, t)
        assert _terminal_count(service) == 50


# ---------------------------------------------------------------------------
# _all_descendants cycle-safety
# ---------------------------------------------------------------------------


def test_all_descendants_is_cycle_safe(service):
    """Directly insert rows where A → B → A (circular parent_id).
    _all_descendants('A') returns [B] without infinite looping."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket

    with db.session(service.settings) as s:
        ta = Ticket(id="cyc-A", title="A", kind="task", workspace_path="")
        tb = Ticket(id="cyc-B", title="B", kind="task", parent_id="cyc-A", workspace_path="")
        s.add_all([ta, tb])
        s.commit()

        # Create the cycle: update A's parent_id to point to B.
        ta.parent_id = "cyc-B"
        s.add(ta)
        s.commit()

    result = service._all_descendants("cyc-A")
    assert len(result) == 1
    assert result[0].id == "cyc-B"
