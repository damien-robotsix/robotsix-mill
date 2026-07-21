import json

import pytest

from robotsix_mill.core.service import (
    AmbiguousTicketId,
    TicketService,
    TransitionError,
    _event_hash,
    _prev_hash_for,
    _slug,
)
from robotsix_mill.core.states import State, can_transition
from robotsix_mill.core.models import SourceKind, TicketKind


def test_slug_strips_dash_exposed_at_truncation_boundary():
    """A title whose post-substitution slug exceeds 40 chars with a dash
    sitting on the truncation boundary must not yield a trailing dash, so
    the resulting ticket ID stays parseable by read_ticket."""
    from secrets import token_hex

    from robotsix_mill.agents.read_ticket import _TICKET_ID_RE

    title = "refactor oversized modules split worker into pieces"
    slug = _slug(title)
    # Reproduces the boundary case: pre-truncation slug ends ...worker-...
    assert slug == "refactor-oversized-modules-split-worker"
    assert not slug.startswith("-")
    assert not slug.endswith("-")

    ticket_id = f"20260609T232401Z-{slug}-{token_hex(2)}"
    assert _TICKET_ID_RE.match(ticket_id) is not None


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
    t = service.create("Explicit source", source=SourceKind.RETROSPECT)
    assert t.source == SourceKind.RETROSPECT


def test_list_filters_by_state(service):
    a = service.create("a")
    service.create("b")
    service.transition(a.id, State.READY)
    assert [t.id for t in service.list(state=State.READY)] == [a.id]
    assert len(service.list(state=State.DRAFT)) == 1
    assert len(service.list()) == 2


def test_reset_engine_disposes_cached_engines(settings):
    """``reset_engine`` must dispose cached engines, not just drop them.

    Leaking undisposed SQLite engines leaks their pooled file
    descriptors; across a full suite run that accumulation eventually
    trips an "unable to open database file" error on a later test.
    """
    from robotsix_mill.core import db

    engine = db.get_engine(settings, board_id="test-board")
    disposed = {"called": False}
    orig_dispose = engine.dispose

    def _spy_dispose(*args, **kwargs):
        disposed["called"] = True
        return orig_dispose(*args, **kwargs)

    engine.dispose = _spy_dispose  # type: ignore[method-assign]
    db.reset_engine()
    assert disposed["called"] is True


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


def test_transition_to_done_rejected_when_ask_user_open(service):
    """A transition to a terminal state (DONE, CLOSED, ERRORED) is
    refused when an open [ASK_USER] thread exists on the ticket."""
    t = service.create("Ask-user still open")
    # DRAFT → DONE is normally allowed (refine's no_change_needed bypass).
    assert can_transition(State.DRAFT, State.DONE)

    # Add an open [ASK_USER] thread.
    c = service.add_comment(t.id, "[ASK_USER]\n\nQuestion?", author="refine")

    # Transition to DONE must be rejected.
    with pytest.raises(TransitionError, match="cannot transition to"):
        service.transition(t.id, State.DONE)

    # Close the thread, then transition succeeds.
    service.close_thread(c.id)
    service.transition(t.id, State.DONE)
    assert service.get(t.id).state is State.DONE


def test_close_open_ask_user_threads_unblocks_terminal(service):
    """close_open_ask_user_threads closes all open [ASK_USER] threads so an
    auto-completing ticket (e.g. merged PR → DONE) can reach the terminal
    state instead of crashing the worker on the open-thread guard."""
    t = service.create("Merged but a question is open")
    service.add_comment(t.id, "[ASK_USER]\n\nQ1?", author="refine")
    service.add_comment(t.id, "[ASK_USER]\n\nQ2?", author="implement")

    with pytest.raises(TransitionError, match="cannot transition to"):
        service.transition(t.id, State.DONE)

    closed = service.close_open_ask_user_threads(t.id)
    assert closed == 2
    # Idempotent: a second call finds nothing left to close.
    assert service.close_open_ask_user_threads(t.id) == 0

    service.transition(t.id, State.DONE)
    assert service.get(t.id).state is State.DONE


def test_transition_to_errored_rejected_when_ask_user_open(service):
    """Same guard applies to ERRORED."""
    t = service.create("Ask-user blocks errored")
    # Take it to READY first — READY → ERRORED is allowed.
    service.transition(t.id, State.READY)
    assert can_transition(State.READY, State.ERRORED)

    c = service.add_comment(t.id, "[ASK_USER]\n\nBlocking?", author="implement")
    with pytest.raises(TransitionError, match="cannot transition to"):
        service.transition(t.id, State.ERRORED)

    service.close_thread(c.id)
    service.transition(t.id, State.ERRORED)
    assert service.get(t.id).state is State.ERRORED


def test_state_machine_edges():
    # draft → ready → deliverable → implement_complete → human_mr_approval(PR) → done(merged) → reviewed
    assert can_transition(State.DRAFT, State.READY)
    assert can_transition(State.READY, State.DELIVERABLE)
    assert can_transition(State.DELIVERABLE, State.IMPLEMENT_COMPLETE)
    assert can_transition(
        State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL
    )  # gates passed
    assert can_transition(State.HUMAN_MR_APPROVAL, State.DONE)  # merged
    assert can_transition(State.HUMAN_MR_APPROVAL, State.BLOCKED)  # closed unmerged
    assert can_transition(
        State.HUMAN_MR_APPROVAL, State.IMPLEMENT_COMPLETE
    )  # silent fallback
    assert can_transition(State.IMPLEMENT_COMPLETE, State.REBASING)  # conflicting PR
    assert can_transition(State.REBASING, State.IMPLEMENT_COMPLETE)  # rebase success
    assert can_transition(State.REBASING, State.BLOCKED)  # rebase exhausted
    assert can_transition(State.REBASING, State.ERRORED)  # rebase crash
    assert can_transition(State.DONE, State.CLOSED)  # retrospected
    assert not can_transition(State.CLOSED, State.DONE)  # terminal
    # deliver-stage no-change bypass: when the branch has no new
    # commits vs origin/main the spec was already satisfied, so route
    # straight to DONE instead of pushing an empty branch and getting
    # a 422 from the forge.
    assert can_transition(State.DELIVERABLE, State.DONE)
    # READY → DONE: implement-stage ``no_change_needed`` bypass.
    assert can_transition(State.READY, State.DONE)


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
    assert not can_transition(State.BLOCKED, State.DELIVERABLE, blocked_from=State.DONE)


# --- REBASING-specific can_transition tests ---


def test_can_transition_covers_rebasing():
    """Verify all new edges involving REBASING."""
    # IMPLEMENT_COMPLETE → REBASING
    assert can_transition(State.IMPLEMENT_COMPLETE, State.REBASING)
    # REBASING → IMPLEMENT_COMPLETE
    assert can_transition(State.REBASING, State.IMPLEMENT_COMPLETE)
    # REBASING → ERRORED
    assert can_transition(State.REBASING, State.ERRORED)
    # REBASING → BLOCKED
    assert can_transition(State.REBASING, State.BLOCKED)
    # REBASING → HUMAN_MR_APPROVAL is NOT allowed (must go through IMPLEMENT_COMPLETE)
    assert not can_transition(State.REBASING, State.HUMAN_MR_APPROVAL)
    # BLOCKED → REBASING with blocked_from=REBASING (resume)
    assert can_transition(State.BLOCKED, State.REBASING, blocked_from=State.REBASING)
    # BLOCKED → REBASING without blocked_from → False
    assert not can_transition(State.BLOCKED, State.REBASING)
    # REBASING → DONE is NOT allowed (must go through IMPLEMENT_COMPLETE → HUMAN_MR_APPROVAL)
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


def test_resume_blocked_with_note_records_comment_and_clears_implement_guard(service):
    """resume_blocked(note=...) persists the note as a comment and, when
    resuming back into READY, deletes a stale artifacts/implement.md,
    artifacts/implement_spawn_count, and implement_conversation_state.json
    so no guard immediately re-blocks the retry and the agent starts a
    fresh conversation that can read corrective feedback."""
    t = service.create("resume with note test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")

    ws = service.workspace(t)
    stale = ws.artifacts_dir / "implement.md"
    stale.write_text("BLOCKED — resumable\nspec-fingerprint: deadbeef\n")
    spawn_counter = ws.artifacts_dir / "implement_spawn_count"
    spawn_counter.write_text("3", encoding="utf-8")
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    conv_state.write_text('{"messages":[]}')

    resumed = service.resume_blocked(t.id, note="retry — prior failure was a flake")
    assert resumed.state is State.READY
    assert not stale.exists()
    assert not spawn_counter.exists()
    assert not conv_state.exists()

    comments = service.list_comments(t.id)
    assert any(
        c.body == "retry — prior failure was a flake" and c.author == "operator"
        for c in comments
    )
    hist = service.history(t.id)
    assert "override: retry — prior failure was a flake" in (hist[-1].note or "")


def test_resume_blocked_without_note_clears_spawn_counter_and_conversation_state(
    service,
):
    """resume_blocked with no note clears artifacts/implement_spawn_count
    when the counter is at/above the spawn limit and clears
    implement_conversation_state.json (any READY resume starts a fresh
    conversation) but leaves the stale-spec guard (artifacts/implement.md)
    untouched — that guard still requires an explicit note."""
    t = service.create("resume without note test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")

    ws = service.workspace(t)
    stale = ws.artifacts_dir / "implement.md"
    stale.write_text("BLOCKED — resumable\nspec-fingerprint: deadbeef\n")
    spawn_counter = ws.artifacts_dir / "implement_spawn_count"
    spawn_counter.write_text("5", encoding="utf-8")
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    conv_state.write_text('{"messages":[]}')

    resumed = service.resume_blocked(t.id)
    assert resumed.state is State.READY
    assert stale.exists()
    assert not spawn_counter.exists()
    assert not conv_state.exists()
    assert service.list_comments(t.id) == []
    hist = service.history(t.id)
    assert "spawn counter reset via resume-blocked" in (hist[-1].note or "")


def test_resume_blocked_below_spawn_limit_preserves_counter(service):
    """resume_blocked does NOT clear the spawn counter when it's below
    the limit — the ticket was blocked from READY for another reason."""
    t = service.create("below spawn limit resume")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="transient OOM in sandbox")

    ws = service.workspace(t)
    spawn_counter = ws.artifacts_dir / "implement_spawn_count"
    spawn_counter.write_text("1", encoding="utf-8")  # < default limit of 3

    resumed = service.resume_blocked(t.id)
    assert resumed.state is State.READY
    assert spawn_counter.exists()
    assert spawn_counter.read_text(encoding="utf-8").strip() == "1"
    hist = service.history(t.id)
    assert "spawn counter reset" not in (hist[-1].note or "")


def test_resume_blocked_spawn_limit_reset_recorded_in_history(service):
    """When the spawn counter IS at the limit, the reset is recorded in
    the event history alongside the standard resume note."""
    t = service.create("spawn limit reset history test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="implement spawn limit reached")

    ws = service.workspace(t)
    spawn_counter = ws.artifacts_dir / "implement_spawn_count"
    spawn_counter.write_text("3", encoding="utf-8")  # at default limit

    resumed = service.resume_blocked(t.id, note="operator inspection done")
    assert resumed.state is State.READY
    assert not spawn_counter.exists()

    hist = service.history(t.id)
    last_note = hist[-1].note or ""
    assert "resumed from blocked" in last_note
    assert "override: operator inspection done" in last_note
    assert "spawn counter reset via resume-blocked" in last_note


def test_resume_blocked_after_retrospect_failure(service):
    """Full scenario: DONE → BLOCKED → resume → DONE → CLOSED.
    This simulates a retrospect failure and proves the ticket can be
    recovered without re-running implement or refine."""
    t = service.create("retrospect fail test")
    # Walk through the pipeline to DONE
    service.transition(t.id, State.READY, note="refined")
    service.transition(t.id, State.DELIVERABLE, note="implemented")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="PR opened, gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="gates passed")
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
    with db.session(service.settings, service.board_id) as s:
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
        State.DRAFT,
        State.HUMAN_ISSUE_APPROVAL,
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.REBASING,
        State.DONE,
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
    ws_dir = settings.workspaces_dir_for(service.board_id) / t.id
    assert ws_dir.exists()

    assert service.get(t.id) is not None
    assert service.history(t.id)  # has events

    assert service.delete(t.id) is True
    assert service.get(t.id) is None
    assert service.history(t.id) == []  # events gone
    assert not ws_dir.exists()  # workspace dir gone


def test_delete_missing_ticket_returns_false(service):
    assert service.delete("does-not-exist") is False


# --- redraft: clean-slate reset ----------------------------------------


def test_redraft_clean_slate_reset(service, settings):
    """redraft folds description + comments + reason into a fresh
    description.md, clears comments/history/branch, and prunes the
    local clone — leaving a single genesis DRAFT event."""
    t = service.create("redraft me", "original description text")
    service.transition(t.id, State.READY)  # adds a second history event
    service.add_comment(t.id, "first comment", author="alice")
    service.add_comment(t.id, "second comment", author="bob")
    service.set_branch(t.id, "feature/redraft-me")

    # Simulate accumulated cost from the prior attempt.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    with _db.session(service.settings, service.board_id) as s:
        row = s.get(_Ticket, t.id)
        row.cost_usd = 4.2
        s.add(row)
        s.commit()

    # Simulate a per-ticket repo clone on disk.
    ws = service.workspace(service.get(t.id))
    ws.repo_dir.mkdir(parents=True, exist_ok=True)
    (ws.repo_dir / "marker").write_text("clone", encoding="utf-8")
    assert ws.repo_dir.exists()

    comment, ticket = service.redraft(t.id, body="because X")

    assert comment is None
    assert service.get(t.id).state is State.DRAFT
    assert service.list_comments(t.id) == []

    hist = service.history(t.id)
    assert len(hist) == 1
    assert hist[0].state is State.DRAFT
    assert hist[0].note == "redrafted: because X"
    assert hist[0].prev_hash is None

    assert service.get(t.id).branch is None
    # Clean slate resets the accumulated cost ledger.
    assert service.get(t.id).cost_usd == 0.0

    # Local clone pruned; workspace dir + description.md remain.
    assert not ws.repo_dir.exists()
    assert ws.dir.exists()
    assert ws.description_path.exists()

    body = service.workspace(service.get(t.id)).read_description()
    assert "original description text" in body
    assert "first comment" in body
    assert "second comment" in body
    assert "because X" in body
    assert "## Folded-in on redraft" in body


def test_redraft_captures_pre_redraft_cost_baseline(service, monkeypatch):
    """redraft snapshots the full Langfuse session cost into
    ``pre_redraft_cost_usd`` (the baseline for the dollar-cap limit)
    while still zeroing the cached ``cost_usd``. A second redraft
    re-snapshots to the then-current full total."""
    import robotsix_mill.langfuse.client as lf_client

    t = service.create("baseline me", "original description text")
    service.transition(t.id, State.READY)

    monkeypatch.setattr(lf_client, "session_cost", lambda *a, **k: 7.5)
    service.redraft(t.id, body="first redraft")
    row = service.get(t.id)
    assert row.pre_redraft_cost_usd == 7.5
    assert row.cost_usd == 0.0

    # A later attempt grows the cumulative session total; the next
    # redraft re-snapshots to that then-current full total.
    service.transition(t.id, State.READY)
    monkeypatch.setattr(lf_client, "session_cost", lambda *a, **k: 12.25)
    service.redraft(t.id, body="second redraft")
    row = service.get(t.id)
    assert row.pre_redraft_cost_usd == 12.25
    assert row.cost_usd == 0.0


def test_redraft_zero_session_cost_baseline_stays_zero(service, monkeypatch):
    """A zero (e.g. unconfigured) live session total leaves the
    ``pre_redraft_cost_usd`` baseline at 0.0 — the no-op baseline path —
    while still zeroing the cached ``cost_usd``."""
    import robotsix_mill.langfuse.client as lf_client

    t = service.create("zero baseline", "original description text")
    service.transition(t.id, State.READY)

    monkeypatch.setattr(lf_client, "session_cost", lambda *a, **k: 0.0)
    service.redraft(t.id, body="zero redraft")
    row = service.get(t.id)
    assert row.pre_redraft_cost_usd == 0.0
    assert row.cost_usd == 0.0


def test_redraft_large_session_cost_baseline_preserved(service, monkeypatch):
    """A large live session total is snapshotted into
    ``pre_redraft_cost_usd`` without truncation or precision loss — the
    field is a plain ``float``."""
    import robotsix_mill.langfuse.client as lf_client

    t = service.create("large baseline", "original description text")
    service.transition(t.id, State.READY)

    monkeypatch.setattr(lf_client, "session_cost", lambda *a, **k: 999999.99)
    service.redraft(t.id, body="large redraft")
    row = service.get(t.id)
    assert row.pre_redraft_cost_usd == 999999.99


def test_redraft_no_comments_empty_body(service):
    """redraft with no comments and an empty body still resets history,
    branch, and clone — but adds no spurious folded-in section."""
    t = service.create("plain redraft", "just the description")
    service.transition(t.id, State.READY)
    service.set_branch(t.id, "feature/plain")
    ws = service.workspace(service.get(t.id))
    ws.repo_dir.mkdir(parents=True, exist_ok=True)

    comment, ticket = service.redraft(t.id)

    assert comment is None
    assert service.get(t.id).state is State.DRAFT
    assert service.get(t.id).branch is None
    assert not ws.repo_dir.exists()

    hist = service.history(t.id)
    assert len(hist) == 1
    assert hist[0].state is State.DRAFT
    assert hist[0].note == "redrafted"
    assert hist[0].prev_hash is None

    body = service.workspace(service.get(t.id)).read_description()
    assert body == "just the description"
    assert "## Folded-in on redraft" not in body


def test_redraft_missing_ticket_raises_keyerror(service):
    with pytest.raises(KeyError):
        service.redraft("does-not-exist")


def test_redraft_non_redraftable_state_raises(service):
    """A DRAFT ticket (in _NON_REDRAFTABLE) cannot be redrafted."""
    t = service.create("already draft")
    assert t.state is State.DRAFT
    with pytest.raises(TransitionError):
        service.redraft(t.id)


def test_redraft_returns_none_comment(service):
    """The returned tuple's first element is always None."""
    t = service.create("redraft return", "desc")
    service.transition(t.id, State.READY)
    comment, ticket = service.redraft(t.id, body="reason")
    assert comment is None
    assert ticket.state is State.DRAFT


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
        "robotsix_mill.core.service._create_mixin.datetime",
        type(
            "m",
            (),
            {
                "now": classmethod(lambda cls, tz=None: fake_now),
                "timezone": dt.timezone,
            },
        )(),
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service._create_mixin.token_hex",
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
    service.transition(dep.id, State.IMPLEMENT_COMPLETE)
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
    service.transition(dep_a.id, State.IMPLEMENT_COMPLETE)
    service.transition(dep_a.id, State.HUMAN_MR_APPROVAL)
    service.transition(dep_a.id, State.DONE)
    service.transition(dep_a.id, State.CLOSED)

    t = service.create("Depender", depends_on=f'["{dep_a.id}", "{dep_b.id}"]')
    unmet = service.unmet_dependencies(t)
    assert unmet == [dep_b.id]


def test_unmet_dependencies_cross_board(settings, service):
    """A dependency on ANOTHER board is gated correctly: ``get()`` fans
    out across every per-repo DB (ticket IDs are globally unique), so
    ``unmet_dependencies`` resolves a foreign-board dep and clears it
    only when that dep reaches a terminal state on its OWN board.

    This backs the scaffold → build-out → migrate flow: a meta-board
    migrate ticket can depend on the build-out ticket filed on the new
    repo's own board, and the gate releases when the build-out is done.
    """
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.service import TicketService as _TS

    _db.init_db(settings, board_id="other-board")
    other = _TS(settings, board_id="other-board")
    dep = other.create("dep on other board")

    t = service.create("Depender", depends_on=f'["{dep.id}"]')
    # dep is DRAFT on the other board → unmet across the board boundary.
    assert service.unmet_dependencies(t) == [dep.id]

    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.DONE,
    ):
        other.transition(dep.id, st)
    # Now terminal on its own board → the cross-board gate releases.
    assert service.unmet_dependencies(t) == []


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

    with core_db.session(service.settings, service.board_id) as s:
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


# ---------------------------------------------------------------------------
# Epic tests
# ---------------------------------------------------------------------------


def test_create_epic(service):
    """Creating with kind='epic' sets state to EPIC_OPEN."""
    t = service.create("My Epic", "Big picture", kind=TicketKind.EPIC)
    assert t.state == State.EPIC_OPEN
    assert t.kind == TicketKind.EPIC


def test_create_child_with_parent_id(service):
    """Creating a child with parent_id links it to the epic."""
    epic = service.create("Epic", "Overview", kind=TicketKind.EPIC)
    child = service.create("Child", "Detail", kind=TicketKind.TASK, parent_id=epic.id)
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
    epic = service.create("Epic", "Big picture description", kind=TicketKind.EPIC)
    child = service.create("Child", "detail", kind=TicketKind.TASK, parent_id=epic.id)
    ctx = service.get_epic_context(child)
    assert (
        ctx == "````epic-context\nBig picture description\n````\n<!-- /epic-context -->"
    )


def test_get_epic_context_no_parent(service):
    """get_epic_context returns '' for a ticket without a parent."""
    t = service.create("Standalone")
    assert service.get_epic_context(t) == ""


def test_get_epic_context_parent_not_epic(service):
    """get_epic_context returns '' when parent is not an epic."""
    parent = service.create("Regular parent", kind=TicketKind.TASK)
    child = service.create("Child", "desc", kind=TicketKind.TASK, parent_id=parent.id)
    assert service.get_epic_context(child) == ""


def test_list_children(service):
    """list_children returns all tickets with the given parent_id."""
    epic = service.create("Epic", "Overview", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", kind=TicketKind.TASK, parent_id=epic.id)
    c2 = service.create("Child 2", kind=TicketKind.TASK, parent_id=epic.id)
    c3 = service.create("Child 3", kind=TicketKind.TASK, parent_id=epic.id)
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
    return len(
        service.list(
            exclude_states=[
                s
                for s in State
                if s not in {State.CLOSED, State.ANSWERED, State.EPIC_CLOSED}
            ]
        )
    )


def _comment_count(service, ticket_id: str) -> int:
    """Return the number of Comment rows for *ticket_id*."""
    from sqlmodel import select

    from robotsix_mill.core import db
    from robotsix_mill.core.models import Comment

    with db.session(service.settings, service._board_for(ticket_id)) as s:
        return len(s.exec(select(Comment).where(Comment.ticket_id == ticket_id)).all())


def _get_comment(service, comment_id: int):
    """Return the Comment row with *comment_id* or None."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Comment

    with db.session(service.settings, service.board_id) as s:
        return s.get(Comment, comment_id)


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
            t = service.create(f"inquiry {i}", kind=TicketKind.INQUIRY)
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
            t = service.create(f"epic {i}", kind=TicketKind.EPIC)
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
# delete cascades to comments
# ---------------------------------------------------------------------------


def test_delete_cascades_to_comments(service):
    """Deleting a ticket also removes its Comment rows."""
    t = service.create("target")
    c = service.add_comment(t.id, "will be cascade-deleted", author="test")

    from robotsix_mill.core import db
    from robotsix_mill.core.models import Comment

    # Confirm it exists before delete.
    with db.session(service.settings, service.board_id) as s:
        assert s.get(Comment, c.id) is not None

    service.delete(t.id)

    # After ticket delete, the comment should be gone.
    with db.session(service.settings, service.board_id) as s:
        assert s.get(Comment, c.id) is None


# ---------------------------------------------------------------------------
# _all_descendants cycle-safety
# ---------------------------------------------------------------------------


def test_all_descendants_is_cycle_safe(service):
    """Directly insert rows where A → B → A (circular parent_id).
    _all_descendants('A') returns [B] without infinite looping."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket

    with db.session(service.settings, service.board_id) as s:
        ta = Ticket(id="cyc-A", title="A", kind=TicketKind.TASK, workspace_path="")
        tb = Ticket(
            id="cyc-B",
            title="B",
            kind=TicketKind.TASK,
            parent_id="cyc-A",
            workspace_path="",
        )
        s.add_all([ta, tb])
        s.commit()

        # Create the cycle: update A's parent_id to point to B.
        ta.parent_id = "cyc-B"
        s.add(ta)
        s.commit()

    result = service._all_descendants("cyc-A")
    assert len(result) == 1
    assert result[0].id == "cyc-B"


def test_transition_no_proposals_clean_transition(service):
    """A ticket transitions cleanly to a terminal state (no error)."""
    t = service.create("No proposals ticket")
    # Should not raise.
    service.transition(t.id, State.DONE)
    service.transition(t.id, State.CLOSED)
    assert service.get(t.id).state is State.CLOSED


# -- mark_done ----------------------------------------------------------


def test_mark_done_from_draft(service):
    """mark_done transitions a DRAFT ticket to DONE and records a
    TicketEvent."""
    t = service.create("mark me done")
    comment, ticket = service.mark_done(t.id)
    assert comment is None
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert hist[-1].state is State.DONE
    assert hist[-1].note == "mark done"


def test_mark_done_from_blocked(service):
    """mark_done transitions a BLOCKED ticket to DONE with a force‑close
    marker in the note."""
    t = service.create("blocked mark done")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    comment, ticket = service.mark_done(t.id)
    assert comment is not None
    assert "[force-closed from blocked] operator mark-done" in comment.body
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert "[force-closed from blocked]" in hist[-1].note


def test_mark_done_from_blocked_with_caller_note(service):
    """When a caller supplies a note on a BLOCKED ticket the force‑close
    marker is prepended and the caller text is preserved."""
    t = service.create("blocked with reason")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    comment, ticket = service.mark_done(t.id, note="PR #123 already merged")
    assert comment is not None
    assert comment.body == "[force-closed from blocked] PR #123 already merged"
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert "[force-closed from blocked] PR #123 already merged" in hist[-1].note


def _make_repo_with_unmerged_branch(service, t, branch: str) -> None:
    """Create a workspace clone whose *branch* carries a commit that is
    NOT on origin/main, so ``verify_merge_before_done`` would raise."""
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)

    def _g(*args):
        _sp.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    _g("init")
    _g("config", "user.email", "test@example.com")
    _g("config", "user.name", "Test")
    (repo / "file.txt").write_text("base")
    _g("add", ".")
    _g("commit", "-m", "initial")
    # origin/main pinned at the base commit.
    _g("branch", "origin/main")
    # A feature branch with an extra, un-merged commit.
    _g("checkout", "-b", branch)
    (repo / "file.txt").write_text("feature change")
    _g("commit", "-am", "unmerged work")
    service.set_branch(t.id, branch)


def test_mark_done_from_blocked_bypasses_merge_verify(service):
    """Escape hatch: an operator can force-close a stuck BLOCKED ticket
    whose branch was never merged. The merge-verification gate (which
    would otherwise 409) is skipped for the deliberate override."""
    t = service.create("blocked no-op loop")
    branch = f"{service.settings.branch_prefix}{t.id}"
    _make_repo_with_unmerged_branch(service, t, branch)
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="PR closed without merge — resumable")

    comment, ticket = service.mark_done(t.id, note="already satisfied on main")
    assert ticket.state is State.DONE
    assert comment is not None
    assert "[force-closed from blocked] already satisfied on main" in comment.body


def test_mark_done_from_rebasing_bypasses_merge_verify(service):
    """Escape hatch also works from REBASING (a ticket wedged in the
    rebase agent), whose branch is conflicting/un-merged."""
    t = service.create("rebasing wedge")
    branch = f"{service.settings.branch_prefix}{t.id}"
    _make_repo_with_unmerged_branch(service, t, branch)
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.REBASING, note="conflicting")

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE
    assert comment is not None
    assert "[force-closed from rebasing] operator mark-done" in comment.body


def test_mark_done_still_verifies_merge_from_normal_state(service):
    """The escape-hatch bypass is scoped to BLOCKED/REBASING only: a
    normal (non-stuck) state with an un-merged branch still refuses
    mark-done so an operator can't prematurely close an open PR."""
    t = service.create("open pr premature close")
    branch = f"{service.settings.branch_prefix}{t.id}"
    _make_repo_with_unmerged_branch(service, t, branch)
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.HUMAN_MR_APPROVAL)

    with pytest.raises(TransitionError, match="not been merged"):
        service.mark_done(t.id)


def test_mark_done_with_note_creates_comment(service):
    """A non-empty note creates a Comment alongside the event."""
    t = service.create("with note")
    comment, ticket = service.mark_done(t.id, note="done manually")
    assert comment is not None
    assert comment.body == "done manually"
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert hist[-1].note == "mark done: done manually"


def test_mark_done_rejects_terminal(service):
    """mark_done raises TransitionError for CLOSED (terminal)."""
    t = service.create("closed ticket")
    # walk to CLOSED
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.HUMAN_MR_APPROVAL)
    service.transition(t.id, State.DONE)
    service.transition(t.id, State.CLOSED)
    with pytest.raises(TransitionError):
        service.mark_done(t.id)


def test_mark_done_rejects_already_done(service):
    """mark_done raises TransitionError for DONE tickets."""
    t = service.create("already done")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.HUMAN_MR_APPROVAL)
    service.transition(t.id, State.DONE)
    with pytest.raises(TransitionError):
        service.mark_done(t.id)


def test_mark_done_rejects_epic_open(service):
    """mark_done raises TransitionError for EPIC_OPEN tickets."""
    t = service.create("epic open", kind=TicketKind.EPIC)
    assert t.state is State.EPIC_OPEN
    with pytest.raises(TransitionError):
        service.mark_done(t.id)


def test_transition_epic_open_to_epic_closed(service):
    """transition() allows EPIC_OPEN → EPIC_CLOSED."""
    t = service.create("abandon me", kind=TicketKind.EPIC)
    assert t.state is State.EPIC_OPEN
    updated = service.transition(t.id, State.EPIC_CLOSED, note="abandoned")
    assert updated.state is State.EPIC_CLOSED


def test_mark_done_auto_closes_open_ask_user_threads(service):
    """mark_done closes open [ASK_USER] threads and records it in the note."""
    t = service.create("Force-closing with open question")
    service.add_comment(t.id, "[ASK_USER]\n\nShould we proceed?", author="implement")

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE
    assert comment is not None
    assert "[force-closed with 1 open [ASK_USER] thread" in comment.body

    # The [ASK_USER] thread must now be closed.
    comments = service.list_comments(t.id)
    ask_comments = [cm for cm in comments if cm.body.startswith("[ASK_USER]")]
    assert len(ask_comments) == 1
    assert ask_comments[0].closed_at is not None


def test_mark_done_no_ask_user_threads_clean_note(service):
    """When there are no open [ASK_USER] threads, the note is unchanged."""
    t = service.create("Clean force-close")
    comment, ticket = service.mark_done(t.id, note="no longer needed")
    assert ticket.state is State.DONE
    assert comment is not None
    assert comment.body == "no longer needed"
    assert "[force-closed" not in comment.body


# -- mark_done citation verification -----------------------------------


def test_mark_done_empty_note_still_works(service):
    """Empty/default note passes through cleanly — no warnings appended."""
    t = service.create("empty note mark done")
    comment, ticket = service.mark_done(t.id)
    assert comment is None
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert hist[-1].note == "mark done"


def test_mark_done_with_nonexistent_pr_appends_warning(service, tmp_path):
    """PR #1562 not on origin/main → ⚠️ appended to comment body."""
    t = service.create("nonexistent pr test")
    # Simulate a workspace clone with a real git repo so git commands
    # can run (origin/main must exist with at least one commit).
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("hello")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True,
        text=True,
    )
    # Create origin/main ref pointing at the initial commit.
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    comment, ticket = service.mark_done(t.id, note="Root cause fixed in PR #1562")
    assert comment is not None
    assert "⚠️" in comment.body
    assert "PR #1562" in comment.body
    assert "not found on origin/main" in comment.body
    assert ticket.state is State.DONE


def test_mark_done_with_existing_pr_stores_cleanly(service, tmp_path):
    """PR #42 found in origin/main commit message → no warning."""
    t = service.create("existing pr test")
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("hello")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "Merge PR #42 into main"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    comment, ticket = service.mark_done(t.id, note="Fixed by PR #42")
    assert ticket.state is State.DONE
    if comment is not None:
        assert "⚠️" not in comment.body


def test_mark_done_with_unverifiable_commit_sha_warns(service, tmp_path):
    """Bogus SHA → ⚠️ appended."""
    t = service.create("bogus sha test")
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("hello")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    comment, ticket = service.mark_done(
        t.id, note="Cherry-picked abcdef1234567890abcdef1234567890abcdef12"
    )
    assert comment is not None
    assert "⚠️" in comment.body
    assert "abcdef1234567890abcdef1234567890abcdef12" in comment.body
    assert "not found on origin/main" in comment.body
    assert ticket.state is State.DONE


def test_mark_done_no_repo_clone_no_crash(service):
    """Missing repo_dir → graceful no-op (note stored verbatim)."""
    t = service.create("no clone test")
    ws = service.workspace(t)
    # Ensure no repo clone exists.
    if ws.repo_dir.exists():
        import shutil

        shutil.rmtree(ws.repo_dir)

    comment, ticket = service.mark_done(t.id, note="Fixed in PR #9999")
    assert ticket.state is State.DONE
    if comment is not None:
        assert comment.body == "Fixed in PR #9999"


# -- Changelog duplicate fragment gate ---------------------------------


def _setup_repo_with_towncrier(repo_dir, fragment_dir_name="changes"):
    """Create a git repo with a pyproject.toml declaring towncrier config."""
    import subprocess as _sp

    repo_dir.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo_dir), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )

    pp = repo_dir / "pyproject.toml"
    pp.write_text(f'[tool.towncrier]\ndirectory = "{fragment_dir_name}"\n')
    _sp.run(
        ["git", "-C", str(repo_dir), "add", "pyproject.toml"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "init with towncrier"],
        capture_output=True,
        text=True,
    )


def _add_fragment(repo_dir, fragment_dir_name, filename, content="fragment content"):
    """Create a fragment file, stage and commit it."""
    import subprocess as _sp

    frag_dir = repo_dir / fragment_dir_name
    frag_dir.mkdir(parents=True, exist_ok=True)
    (frag_dir / filename).write_text(content)
    _sp.run(
        ["git", "-C", str(repo_dir), "add", f"{fragment_dir_name}/{filename}"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "commit", "-m", f"add {filename}"],
        capture_output=True,
        text=True,
    )


def _setup_repo_with_branch(repo_dir, ticket_id, branch_prefix="mill/"):
    """Create a git repo with an initial commit on origin/main and a
    feature branch <branch_prefix><ticket_id>.

    Returns (repo_dir, branch_name).
    """
    import subprocess as _sp

    repo_dir.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo_dir), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo_dir / "README.md").write_text("initial")
    _sp.run(["git", "-C", str(repo_dir), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "initial commit"],
        capture_output=True,
        text=True,
    )
    # Create origin/main as a local ref (simulates remote tracking branch).
    _sp.run(
        ["git", "-C", str(repo_dir), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )
    branch = f"{branch_prefix}{ticket_id}"
    _sp.run(
        ["git", "-C", str(repo_dir), "checkout", "-b", branch],
        capture_output=True,
        text=True,
    )
    return branch


def _advance_origin_main(repo_dir):
    """Point origin/main to the current HEAD."""
    import subprocess as _sp

    _sp.run(
        ["git", "-C", str(repo_dir), "branch", "-f", "origin/main"],
        capture_output=True,
        text=True,
    )


# -- mark_done merge verification ---------------------------------------


def test_mark_done_merge_ancestor_succeeds(service, tmp_path):
    """When the feature branch tip is an ancestor of origin/main,
    mark_done succeeds."""
    t = service.create("merge ancestor test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch.
    (repo / "file.txt").write_text("feature work")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "feature commit"],
        capture_output=True,
        text=True,
    )

    # Fast-forward origin/main to include the feature branch (simulate merge).
    _advance_origin_main(repo)

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_merge_squash_detected(service, tmp_path):
    """When the branch tip is NOT an ancestor but a squash-merge commit
    referencing the ticket ID exists on origin/main, mark_done succeeds."""
    t = service.create("squash merge test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch.
    (repo / "file.txt").write_text("feature work")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "feature commit"],
        capture_output=True,
        text=True,
    )

    # Simulate squash-merge: create a new commit on origin/main that
    # references the ticket ID but is NOT a merge of the feature branch.
    _sp.run(
        ["git", "-C", str(repo), "checkout", "origin/main"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("feature work")  # same content
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "-m",
            f"Squash merge #{t.id} into main",
        ],
        capture_output=True,
        text=True,
    )
    _advance_origin_main(repo)

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_merge_content_match(service, tmp_path):
    """When the branch tip is NOT an ancestor and no log grep match,
    but a changed file on origin/main contains the ticket ID,
    mark_done succeeds (content-level fallback)."""
    t = service.create("content match test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch that includes the ticket ID
    # in file content.
    (repo / "changelog.md").write_text(f"## {t.id}\n\nFeature work done.")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "add changelog"],
        capture_output=True,
        text=True,
    )

    # Simulate a cherry-pick / rebase: apply the same content on
    # origin/main without the ticket ID in the commit message.
    _sp.run(
        ["git", "-C", str(repo), "checkout", "origin/main"],
        capture_output=True,
        text=True,
    )
    (repo / "changelog.md").write_text(f"## {t.id}\n\nFeature work done.")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "apply changelog"],
        capture_output=True,
        text=True,
    )
    _advance_origin_main(repo)

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_merge_not_verified_raises(service, tmp_path):
    """When all merge checks fail, mark_done raises TransitionError."""
    t = service.create("not merged test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch that origin/main does NOT have.
    (repo / "file.txt").write_text("unmerged work")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "unmerged commit"],
        capture_output=True,
        text=True,
    )

    # origin/main stays at the initial commit — no merge happened.

    with pytest.raises(TransitionError, match="has not been merged"):
        service.mark_done(t.id)


def test_mark_done_merge_no_branch_skips_verification(service, tmp_path):
    """When the feature branch doesn't exist locally, verification is
    skipped and mark_done succeeds (best-effort)."""
    t = service.create("no branch test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    # Create a repo with origin/main but NO feature branch.
    import subprocess as _sp

    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("initial")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_rejects_duplicate_changelog_fragments(service, tmp_path):
    """mark_done raises TransitionError when the branch HEAD has
    >1 fragment for the ticket id."""
    t = service.create("dupe frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    _add_fragment(repo, "changes", f"{t.id}.feature.md")
    _add_fragment(repo, "changes", f"{t.id}.misc.md")

    with pytest.raises(TransitionError, match="duplicate changelog fragments"):
        service.mark_done(t.id)


def test_mark_done_allows_single_changelog_fragment(service, tmp_path):
    """mark_done succeeds when only one fragment exists for the ticket."""
    t = service.create("single frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    _add_fragment(repo, "changes", f"{t.id}.misc.md")

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_allows_no_changelog_fragments(service, tmp_path):
    """mark_done succeeds when no fragment exists for the ticket."""
    t = service.create("no frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    # no fragment added

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_allows_no_towncrier_config(service, tmp_path):
    """mark_done succeeds (best-effort) when pyproject.toml has no
    [tool.towncrier] section."""
    t = service.create("no tc config")
    ws = service.workspace(t)
    repo = ws.repo_dir

    import subprocess as _sp

    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "pyproject.toml").write_text('[project]\nname = "test"\n')
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        capture_output=True,
        text=True,
    )
    # Add a fragment anyway (no towncrier config → gate should skip).
    _add_fragment(repo, "changes", f"{t.id}.feature.md")

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_transition_to_done_rejects_duplicate_changelog_fragments(service, tmp_path):
    """transition(..., DONE) raises TransitionError when the branch HEAD
    has >1 fragment for the ticket id."""
    t = service.create("transition dupe frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    _add_fragment(repo, "changes", f"{t.id}.feature.md")
    _add_fragment(repo, "changes", f"{t.id}.misc.md")

    # DRAFT -> DONE is normally allowed; the fragment gate blocks it.
    assert can_transition(t.state, State.DONE)
    with pytest.raises(TransitionError, match="duplicate changelog fragments"):
        service.transition(t.id, State.DONE)


# --- Epic-priority propagation ----------------------------------------


def test_set_priority_propagates_to_existing_children(service):
    """When an epic is flagged priority, every existing descendant
    inherits the flag — and set_priority returns the IDs that changed
    so the route can re-enqueue each one."""
    epic = service.create("an epic", kind=TicketKind.EPIC)
    a = service.create("child a", parent_id=epic.id)
    b = service.create("child b", parent_id=epic.id)
    grand = service.create("grand", parent_id=a.id)

    changed = service.set_priority(epic.id, True)
    assert set(changed) == {epic.id, a.id, b.id, grand.id}
    for tid in (epic.id, a.id, b.id, grand.id):
        assert service.get(tid).priority is True


def test_set_priority_false_clears_descendants(service):
    """Flipping an epic back to non-priority clears the same set."""
    epic = service.create("an epic", kind=TicketKind.EPIC)
    a = service.create("child a", parent_id=epic.id)
    service.set_priority(epic.id, True)
    assert service.get(a.id).priority is True

    changed = service.set_priority(epic.id, False)
    assert set(changed) == {epic.id, a.id}
    assert service.get(a.id).priority is False


def test_set_priority_returns_only_actually_changed(service):
    """If a descendant already has the target value, it's not in the
    returned list (no needless re-enqueue)."""
    epic = service.create("an epic", kind=TicketKind.EPIC)
    a = service.create("child a", parent_id=epic.id)
    service.set_priority(a.id, True)  # child already priority
    changed = service.set_priority(epic.id, True)
    assert epic.id in changed
    assert a.id not in changed  # already True; no change


def test_set_priority_broadcasts_each_changed_ticket(service):
    """set_priority fires _on_transition exactly for the tickets whose
    flag actually flipped (target + flipped descendants) — so the board
    UI updates live over the WebSocket without a manual refresh."""
    from robotsix_mill.core.models import Ticket

    epic = service.create("an epic", kind=TicketKind.EPIC)
    a = service.create("child a", parent_id=epic.id)
    service.create("child b", parent_id=epic.id)
    service.create("grand", parent_id=a.id)

    seen: list[str] = []
    recorded: list[Ticket] = []

    def recorder(ticket: Ticket) -> None:
        seen.append(ticket.id)
        recorded.append(ticket)

    service._on_transition = recorder
    changed = service.set_priority(epic.id, True)

    # Fired once per changed ticket — no duplicates, no extras.
    assert sorted(seen) == sorted(changed)
    assert len(seen) == len(set(seen))
    # The recorded objects are usable by broadcast_sync.
    assert all(t.priority is True for t in recorded)


def test_set_priority_no_broadcast_when_nothing_flips(service):
    """When the target already holds the requested value (and no
    descendant flips), _on_transition is not invoked at all."""
    t = service.create("a task")

    seen: list[str] = []
    service._on_transition = lambda ticket: seen.append(ticket.id)
    changed = service.set_priority(t.id, False)  # already False

    assert changed == []
    assert seen == []


def test_child_created_after_epic_priority_inherits(service):
    """A ticket created with parent_id pointing at a priority-flagged
    epic inherits the flag at create time — the key case for
    multi-stage breakdowns that arrive after the operator marks the
    epic priority."""
    epic = service.create("an epic", kind=TicketKind.EPIC)
    service.set_priority(epic.id, True)

    late_child = service.create("created after", parent_id=epic.id)
    assert late_child.priority is True


def test_inheritance_walks_full_parent_chain(service):
    """Grandchild of a priority epic inherits via transitive walk."""
    epic = service.create("an epic", kind=TicketKind.EPIC)
    service.set_priority(epic.id, True)
    child = service.create("child", parent_id=epic.id)
    grand = service.create("grand", parent_id=child.id)
    assert grand.priority is True


def test_no_inheritance_when_no_priority_ancestor(service):
    """When the parent chain has no priority flag, the new ticket
    defaults to non-priority."""
    epic = service.create("an epic", kind=TicketKind.EPIC)  # not priority
    child = service.create("child", parent_id=epic.id)
    assert child.priority is False


def test_explicit_priority_at_create(service):
    ticket = service.create("urgent task", priority=True)
    assert ticket.priority is True


def test_explicit_priority_composes_with_inheritance(service):
    """Explicit priority=True on a child of a priority epic still yields True."""
    epic = service.create("an epic", kind=TicketKind.EPIC)
    service.set_priority(epic.id, True)
    child = service.create("child", parent_id=epic.id, priority=True)
    assert child.priority is True


# --- ask_user close-thread → auto-resume --------------------------------


def test_transition_to_awaiting_user_reply_sets_paused_from(service):
    """``transition()`` to AWAITING_USER_REPLY auto-populates
    ``paused_from`` to the pre-pause state value — no caller
    kwarg required (regression test for commit 170f7991)."""
    t = service.create("paused_from auto test")
    service.transition(t.id, State.READY)

    # Transition to AWAITING_USER_REPLY — no explicit paused_from
    service.transition(t.id, State.AWAITING_USER_REPLY)

    reloaded = service.get(t.id)
    assert reloaded.state is State.AWAITING_USER_REPLY
    assert reloaded.paused_from == State.READY.value


def test_close_thread_resumes_when_all_ask_user_closed(service):
    """AC1: Closing the last open [ASK_USER] thread on a paused ticket
    auto-resumes it to paused_from."""
    t = service.create("Resume test")
    service.transition(t.id, State.READY)

    # Pause the ticket from READY.
    service.transition(t.id, State.AWAITING_USER_REPLY)
    reloaded = service.get(t.id)
    assert reloaded.state is State.AWAITING_USER_REPLY
    assert reloaded.paused_from == State.READY.value

    # Create an [ASK_USER] thread.
    c = service.add_comment(t.id, "[ASK_USER]\n\nWhat should I do?", author="refine")
    assert c.parent_id is None

    # Close it → should auto-resume.
    result = service.close_thread(c.id)
    assert result.closed_at is not None
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    assert reloaded.paused_from is None
    assert reloaded.blocked_from is None

    # History has the resume event.
    events = service.history(t.id)
    assert any("all ask_user threads closed" in (ev.note or "") for ev in events)


def test_close_thread_stays_paused_when_ask_user_still_open(service):
    """AC1: Closing one [ASK_USER] thread when another is still open
    keeps the ticket in AWAITING_USER_REPLY."""
    t = service.create("Multi-ask test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)
    assert service.get(t.id).state is State.AWAITING_USER_REPLY

    # Two [ASK_USER] threads.
    c1 = service.add_comment(t.id, "[ASK_USER]\n\nFirst question?", author="refine")
    c2 = service.add_comment(t.id, "[ASK_USER]\n\nSecond question?", author="implement")

    # Close only one.
    service.close_thread(c1.id)
    reloaded = service.get(t.id)
    assert reloaded.state is State.AWAITING_USER_REPLY  # still paused

    # Close the second → now resumes.
    service.close_thread(c2.id)
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY


def test_close_thread_non_ask_user_on_paused_no_resume(service):
    """AC3: Closing a non-[ASK_USER] thread on a paused ticket does NOT
    trigger resume."""
    t = service.create("Non-ask close test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask = service.add_comment(t.id, "[ASK_USER]\n\nQuestion?", author="refine")
    normal = service.add_comment(t.id, "Just a normal thread", author="alice")

    # Close the normal thread — should NOT resume.
    service.close_thread(normal.id)
    assert service.get(t.id).state is State.AWAITING_USER_REPLY

    # Close the ask thread → now resumes.
    service.close_thread(ask.id)
    assert service.get(t.id).state is State.READY


def test_close_thread_on_non_paused_ticket_unchanged(service):
    """AC6: close_thread on a non-paused ticket does not trigger any
    resume — behaves exactly as before."""
    t = service.create("Normal close")
    service.transition(t.id, State.READY)

    c = service.add_comment(t.id, "[ASK_USER]\n\nA question?", author="refine")
    service.close_thread(c.id)

    # Ticket state unchanged.
    assert service.get(t.id).state is State.READY


def test_close_thread_with_no_reply(service):
    """AC5: Close an [ASK_USER] thread with no child replies still
    resumes. The _collect_ask_user_replies helper handles the
    '(closed without reply)' case."""
    t = service.create("No reply test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask = service.add_comment(t.id, "[ASK_USER]\n\nSilent question?", author="refine")
    # Close without any reply comments.
    service.close_thread(ask.id)
    assert service.get(t.id).state is State.READY


def test_reopen_after_resume_no_effect(service):
    """AC4: Reopening an [ASK_USER] thread after the ticket has already
    resumed does not re-pause the ticket."""
    t = service.create("Reopen after resume")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask = service.add_comment(t.id, "[ASK_USER]\n\nQuestion?", author="refine")
    service.close_thread(ask.id)
    assert service.get(t.id).state is State.READY  # resumed

    # Reopen the thread — ticket stays READY.
    service.reopen_thread(ask.id)
    assert service.get(t.id).state is State.READY


def test_pause_and_resume_full_cycle(service):
    """Full pause→close→resume cycle across multiple questions."""
    t = service.create("Full cycle")
    service.transition(t.id, State.READY)

    # First pause
    service.transition(t.id, State.AWAITING_USER_REPLY)
    assert service.get(t.id).paused_from == State.READY.value

    ask1 = service.add_comment(t.id, "[ASK_USER]\n\nQ1?", author="refine")
    service.close_thread(ask1.id)
    assert service.get(t.id).state is State.READY  # resumed

    # Second pause (agent asks again after resume)
    service.transition(t.id, State.AWAITING_USER_REPLY)
    assert service.get(t.id).paused_from == State.READY.value

    ask2 = service.add_comment(t.id, "[ASK_USER]\n\nQ2?", author="implement")
    service.close_thread(ask2.id)
    assert service.get(t.id).state is State.READY  # resumed again


def test_awaiting_user_reply_without_paused_from_recovers_from_history(service):
    """AWAITING_USER_REPLY with no paused_from (legacy pre-170f7991)
    recovers the pre-pause state from event history instead of
    staying stranded."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket

    t = service.create("Legacy paused")
    # Simulate a ticket that was paused via transition() (which wrote the
    # AWAITING_USER_REPLY event) but whose paused_from was lost (pre-170f7991).
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    # Manually clear paused_from to simulate pre-170f7991 state.
    with db.session(service.settings, service.board_id) as s:
        ticket = s.get(Ticket, t.id)
        ticket.paused_from = None
        s.add(ticket)
        s.commit()

    ask = service.add_comment(t.id, "[ASK_USER]\n\nQ?", author="refine")
    # Should recover from event history, not raise.
    result = service.close_thread(ask.id)
    assert result.closed_at is not None
    # Ticket resumes to READY (the state before AWAITING_USER_REPLY in history).
    assert service.get(t.id).state is State.READY
    assert service.get(t.id).paused_from is None


def test_awaiting_user_reply_no_prior_events_no_recovery(service):
    """When a ticket is in AWAITING_USER_REPLY with no paused_from AND
    no prior events exist (e.g. direct DB corruption beyond the legacy
    case), the fallback logs a warning and does NOT resume."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket, TicketEvent
    from sqlmodel import select

    t = service.create("No prior events")
    # Delete all events to simulate a ticket with no history at all.
    with db.session(service.settings, service.board_id) as s:
        for ev in s.exec(
            select(TicketEvent).where(TicketEvent.ticket_id == t.id)
        ).all():
            s.delete(ev)
        ticket = s.get(Ticket, t.id)
        ticket.state = State.AWAITING_USER_REPLY
        ticket.paused_from = None
        s.add(ticket)
        s.commit()

    ask = service.add_comment(t.id, "[ASK_USER]\n\nQ?", author="refine")
    result = service.close_thread(ask.id)
    assert result.closed_at is not None
    # No prior events → cannot recover; stays AWAITING_USER_REPLY.
    assert service.get(t.id).state is State.AWAITING_USER_REPLY


def test_legacy_paused_from_recovery_via_event_history(service):
    """A legacy ticket with paused_from=None (stranded pre-170f7991)
    auto-resumes by recovering the pre-pause state from event history
    when its last [ASK_USER] thread is closed."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket

    t = service.create("legacy recovery test")
    service.transition(t.id, State.READY)

    # Add [ASK_USER] comment
    c = service.add_comment(t.id, "[ASK_USER]\n\nWhat branch?", author="implement")

    # Transition to AWAITING_USER_REPLY (this sets paused_from via the
    # _lifecycle fix, creating an AWAITING_USER_REPLY event row).
    service.transition(t.id, State.AWAITING_USER_REPLY)
    assert service.get(t.id).paused_from == State.READY.value

    # Simulate pre-170f7991: clear paused_from in the DB while the
    # event row for AWAITING_USER_REPLY still exists.
    with db.session(service.settings, service.board_id) as s:
        ticket = s.get(Ticket, t.id)
        ticket.paused_from = None
        s.add(ticket)
        s.commit()

    # Verify the corruption took effect.
    assert service.get(t.id).paused_from is None

    # Close the thread → should recover from event history.
    service.close_thread(c.id, ticket_id=t.id)

    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    assert reloaded.paused_from is None


def test_close_thread_no_ask_user_threads_on_paused_no_resume(service):
    """If a ticket is AWAITING_USER_REPLY but has no [ASK_USER] threads
    at all, close_thread should not crash and should not transition."""
    t = service.create("Paused no ask")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    # Create a normal thread (not [ASK_USER]).
    normal = service.add_comment(t.id, "Normal thread", author="alice")
    service.close_thread(normal.id)

    # Still paused — no [ASK_USER] threads were found.
    assert service.get(t.id).state is State.AWAITING_USER_REPLY


# --- _collect_ask_user_replies -----------------------------------------


def test_collect_ask_user_replies_single_thread(service, settings):
    """AC2: Single [ASK_USER] thread with one reply."""
    from robotsix_mill.stages.pause import _collect_ask_user_replies
    from robotsix_mill.stages.base import StageContext

    t = service.create("Single ask")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask = service.add_comment(
        t.id, "[ASK_USER]\n\nWhat is the answer?", author="refine"
    )
    service.add_comment(t.id, "The answer is 42.", author="operator", parent_id=ask.id)
    service.close_thread(ask.id)

    ctx = StageContext(settings=settings, service=service)
    result = _collect_ask_user_replies(ctx, t)
    assert "What is the answer?" in result
    assert "The answer is 42." in result


def test_collect_ask_user_replies_multiple_threads(service, settings):
    """AC2: Multiple [ASK_USER] threads, each with replies."""
    from robotsix_mill.stages.pause import _collect_ask_user_replies
    from robotsix_mill.stages.base import StageContext

    t = service.create("Multi ask")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask1 = service.add_comment(t.id, "[ASK_USER]\n\nQ1?", author="refine")
    ask2 = service.add_comment(t.id, "[ASK_USER]\n\nQ2?", author="implement")
    service.add_comment(t.id, "R1", author="op", parent_id=ask1.id)
    service.add_comment(t.id, "R2a", author="op", parent_id=ask2.id)
    service.add_comment(t.id, "R2b", author="op", parent_id=ask2.id)
    service.close_thread(ask1.id)
    service.close_thread(ask2.id)

    ctx = StageContext(settings=settings, service=service)
    result = _collect_ask_user_replies(ctx, t)
    assert "Q1?" in result
    assert "R1" in result
    assert "Q2?" in result
    assert "R2a" in result
    assert "R2b" in result


def test_collect_ask_user_replies_closed_without_reply(service, settings):
    """AC5: [ASK_USER] thread closed with no child comments."""
    from robotsix_mill.stages.pause import _collect_ask_user_replies
    from robotsix_mill.stages.base import StageContext

    t = service.create("No reply collect")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask = service.add_comment(t.id, "[ASK_USER]\n\nSilent?", author="refine")
    service.close_thread(ask.id)

    ctx = StageContext(settings=settings, service=service)
    result = _collect_ask_user_replies(ctx, t)
    assert "Silent?" in result
    assert "(closed without reply)" in result


def test_collect_ask_user_replies_skips_open_ask_threads(service, settings):
    """Only closed [ASK_USER] threads contribute replies; open ones are
    skipped."""
    from robotsix_mill.stages.pause import _collect_ask_user_replies
    from robotsix_mill.stages.base import StageContext

    t = service.create("Mixed open closed")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask1 = service.add_comment(t.id, "[ASK_USER]\n\nClosed Q?", author="refine")
    service.add_comment(t.id, "[ASK_USER]\n\nOpen Q?", author="implement")
    service.add_comment(t.id, "Reply", author="op", parent_id=ask1.id)
    service.close_thread(ask1.id)
    # ask2 stays open

    ctx = StageContext(settings=settings, service=service)
    result = _collect_ask_user_replies(ctx, t)
    # Only closed Q appears.
    assert "Closed Q?" in result
    assert "Reply" in result
    assert "Open Q?" not in result


def test_collect_ask_user_replies_no_answered_threads(service, settings):
    """When no [ASK_USER] threads are closed, returns fallback."""
    from robotsix_mill.stages.pause import _collect_ask_user_replies
    from robotsix_mill.stages.base import StageContext

    t = service.create("No answered")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    service.add_comment(t.id, "[ASK_USER]\n\nStill open?", author="refine")
    # Not closed.

    ctx = StageContext(settings=settings, service=service)
    result = _collect_ask_user_replies(ctx, t)
    assert result == "(no operator reply found)"


# --- pending_question ---------------------------------------------------


def test_pending_question_returns_verbatim_question_text(service):
    """pending_question returns the question text from the latest open
    [ASK_USER] comment, with the marker stripped."""
    t = service.create("Pending question test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    service.add_comment(
        t.id, "[ASK_USER]\n\nWhat color should the button be?", author="refine"
    )

    result = service.pending_question(t.id)
    assert result == "What color should the button be?"


def test_pending_question_none_when_no_open_ask_user(service):
    """pending_question returns None when there are no [ASK_USER] comments."""
    t = service.create("No ask user")
    assert service.pending_question(t.id) is None


def test_pending_question_none_when_thread_closed(service):
    """pending_question returns None when the only [ASK_USER] thread is closed."""
    t = service.create("Closed ask")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    c = service.add_comment(
        t.id, "[ASK_USER]\n\nWhat is your favorite color?", author="refine"
    )
    service.close_thread(c.id)

    # After closing the thread, pending_question should be None
    # (the ticket also auto-resumes, but the field is computed at read time)
    assert service.pending_question(t.id) is None


def test_pending_question_latest_of_many_open_threads(service):
    """When multiple open [ASK_USER] threads exist, the most recent is returned."""
    t = service.create("Multi ask")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    service.add_comment(t.id, "[ASK_USER]\n\nFirst question?", author="refine")
    service.add_comment(
        t.id, "[ASK_USER]\n\nSecond newer question?", author="implement"
    )

    result = service.pending_question(t.id)
    assert result == "Second newer question?"


def test_pending_question_skips_non_ask_user_threads(service):
    """pending_question only considers threads whose body starts with [ASK_USER]."""
    t = service.create("Mixed threads")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    service.add_comment(t.id, "Just a normal thread", author="alice")
    service.add_comment(t.id, "[ASK_USER]\n\nThis is the question", author="refine")

    result = service.pending_question(t.id)
    assert result == "This is the question"


def test_pending_question_handles_marker_only_body(service):
    """pending_question handles a body that is just the marker with no newline."""
    t = service.create("Marker only")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    service.add_comment(t.id, "[ASK_USER]", author="refine")

    result = service.pending_question(t.id)
    assert result == ""


# ---------------------------------------------------------------------------
# hash-chain helpers (_event_hash, _prev_hash_for, _make_event)
# ---------------------------------------------------------------------------


def test_event_hash_is_deterministic():
    """Same inputs produce the same hash."""
    h1 = _event_hash("t1", "draft", "created", "2025-01-01T00:00:00+00:00", None)
    h2 = _event_hash("t1", "draft", "created", "2025-01-01T00:00:00+00:00", None)
    assert h1 == h2


def test_event_hash_is_sensitive_to_every_field():
    """Changing any payload field changes the hash."""
    base = _event_hash("t1", "draft", "note", "2025-01-01T00:00:00+00:00", "prev")
    assert base != _event_hash(
        "t2", "draft", "note", "2025-01-01T00:00:00+00:00", "prev"
    )
    assert base != _event_hash(
        "t1", "ready", "note", "2025-01-01T00:00:00+00:00", "prev"
    )
    assert base != _event_hash(
        "t1", "draft", "other", "2025-01-01T00:00:00+00:00", "prev"
    )
    assert base != _event_hash(
        "t1", "draft", "note", "2025-01-02T00:00:00+00:00", "prev"
    )
    assert base != _event_hash(
        "t1", "draft", "note", "2025-01-01T00:00:00+00:00", "prev2"
    )


def test_event_hash_none_note_and_prev_hash():
    """None note and None prev_hash are handled consistently."""
    h1 = _event_hash("t1", "draft", None, "2025-01-01T00:00:00+00:00", None)
    h2 = _event_hash("t1", "draft", None, "2025-01-01T00:00:00+00:00", None)
    assert h1 == h2
    # None note vs non-None produce different hashes
    assert h1 != _event_hash(
        "t1", "draft", "something", "2025-01-01T00:00:00+00:00", None
    )


def test_event_hash_format_is_hex():
    """Hash is 64 hex chars (BLAKE2b 32-byte)."""
    h = _event_hash("t1", "draft", None, "2025-01-01T00:00:00+00:00", None)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_event_hash_canonical_json_is_compact():
    """Internal canonical JSON uses compact separators."""
    payload = {
        "ticket_id": "t1",
        "state": "draft",
        "note": None,
        "at": "2025-01-01T00:00:00+00:00",
        "prev_hash": None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # compact JSON has no spaces
    assert " " not in canonical
    assert '"ticket_id":"t1"' in canonical


def test_make_event_populates_hash_and_prev_hash(service):
    """_make_event sets prev_hash from the most recent event and computes hash."""
    # Create a first event (through the service, which uses _make_event).
    t = service.create("hash-chain test")
    events = service.history(t.id)
    assert len(events) == 1
    assert events[0].prev_hash is None
    assert events[0].hash

    # Create a second event.
    service.transition(t.id, State.READY, note="refined")
    events = service.history(t.id)
    assert len(events) == 2
    assert events[1].prev_hash == events[0].hash
    assert events[1].hash != events[0].hash
    # The second event's hash can be recomputed and matches.
    recomputed = _event_hash(
        ticket_id=t.id,
        state=events[1].state.value,
        note=events[1].note,
        at=events[1].at.isoformat(),
        prev_hash=events[1].prev_hash,
    )
    assert recomputed == events[1].hash


def test_make_event_prev_hash_chains_across_three_events(service):
    """Three-event chain: each event's prev_hash matches the prior event's hash."""
    t = service.create("three-link chain")
    service.transition(t.id, State.READY, note="link 2")
    service.transition(t.id, State.DELIVERABLE, note="link 3")
    events = service.history(t.id)
    assert len(events) == 3
    assert events[0].prev_hash is None
    assert events[1].prev_hash == events[0].hash
    assert events[2].prev_hash == events[1].hash


def test_make_event_hash_differs_per_ticket(service):
    """Two tickets with identical states produce different hashes."""
    t1 = service.create("chain-a")
    t2 = service.create("chain-b")
    e1 = service.history(t1.id)[0]
    e2 = service.history(t2.id)[0]
    assert e1.hash != e2.hash


def test_prev_hash_for_returns_none_for_new_ticket(service):
    """_prev_hash_for returns None when a ticket has no events."""
    from robotsix_mill.core import db

    with db.session(service.settings, service.board_id) as s:
        assert _prev_hash_for(s, "nonexistent-ticket") is None


def test_make_event_state_value_stored_as_enum(service):
    """The state of an event created by _make_event is a State enum member."""
    t = service.create("state as enum")
    events = service.history(t.id)
    assert isinstance(events[0].state, State)


# ---------------------------------------------------------------------------
# add_history_note: side-band events for trace breadcrumbs
# ---------------------------------------------------------------------------


def test_add_history_note_appends_event_at_current_state(service):
    """add_history_note writes a TicketEvent at the ticket's CURRENT
    state (no transition) and chains into the existing hash chain."""
    t = service.create("trace-target")
    pre = service.history(t.id)
    assert len(pre) == 1
    assert pre[0].state is State.DRAFT

    event = service.add_history_note(t.id, "🔍 [Trace: refine](https://example/x)")

    assert event.state is State.DRAFT  # same state — no transition
    assert event.note == "🔍 [Trace: refine](https://example/x)"

    post = service.history(t.id)
    assert len(post) == 2
    assert post[-1].id == event.id
    # Hash chain intact: the new event's prev_hash matches the prior
    # event's hash.
    assert post[-1].prev_hash == post[0].hash
    # The ticket's state is unchanged.
    assert service.get(t.id).state is State.DRAFT


def test_add_history_note_unknown_ticket_raises(service):
    """KeyError for a non-existent ticket id — parity with transition()."""
    import pytest as _pytest

    with _pytest.raises(KeyError):
        service.add_history_note("does-not-exist", "irrelevant")


# ---------------------------------------------------------------------------
# unblocks: a solver auto-reopens BLOCKED tickets when it completes
# ---------------------------------------------------------------------------


def test_create_with_unblocks_is_stored(service):
    t = service.create("solver", unblocks=json.dumps(["a", "b"]))
    assert json.loads(service.get(t.id).unblocks) == ["a", "b"]


def test_set_unblocks_dedups_and_drops_self(service):
    solver = service.create("solver")
    t = service.set_unblocks(solver.id, [solver.id, "x", "x", "y"])
    assert json.loads(t.unblocks) == ["x", "y"]  # self dropped, dups collapsed
    # empty list clears the field
    assert service.set_unblocks(solver.id, []).unblocks is None


def test_unblocks_fires_on_done(service):
    solver = service.create("solver")
    target = service.create("target")
    service.transition(target.id, State.BLOCKED)
    service.set_unblocks(solver.id, [target.id])

    service.transition(solver.id, State.DONE)  # DRAFT -> DONE is allowed

    assert service.get(target.id).state is State.DRAFT  # auto-unblocked
    draft_notes = [e.note for e in service.history(target.id) if e.state is State.DRAFT]
    assert any(solver.id in (n or "") for n in draft_notes)


def test_unblocks_skips_target_that_is_not_blocked(service):
    solver = service.create("solver")
    target = service.create("target")
    service.transition(target.id, State.READY)  # not BLOCKED
    service.set_unblocks(solver.id, [target.id])

    service.transition(solver.id, State.DONE)

    assert service.get(target.id).state is State.READY  # left untouched


def test_unblocks_not_fired_on_non_terminal_transition(service):
    solver = service.create("solver")
    target = service.create("target")
    service.transition(target.id, State.BLOCKED)
    service.set_unblocks(solver.id, [target.id])

    service.transition(solver.id, State.READY)  # not a completion state

    assert service.get(target.id).state is State.BLOCKED  # still blocked


# -- _board_for / _board_for_comment error paths ----------------------------


def test_board_for_raises_on_not_found_and_empty_board_id(settings):
    """When self.board_id is empty and the ticket doesn't exist in any
    board, _board_for raises a clear ValueError instead of returning "".
    """
    svc = TicketService(settings)  # no board_id → board_id=""
    with pytest.raises(
        ValueError, match=r"Ticket nonexistent-id not found.*\(searched:"
    ):
        svc._board_for("nonexistent-id")


def test_board_for_comment_raises_on_not_found_and_empty_board_id(settings):
    """When ticket_id is None, no board contains the comment, and
    self.board_id is empty, _board_for_comment raises ValueError.
    """
    svc = TicketService(settings)  # no board_id → board_id=""
    with pytest.raises(ValueError, match=r"Comment 999 not found.*\(searched:"):
        svc._board_for_comment(999, ticket_id=None)


def test_board_for_returns_bound_board_when_ticket_found_locally(service):
    """When the service is bound to a board and the ticket exists there,
    _board_for returns that board_id directly (no fanout)."""
    t = service.create("board-for-local-test")
    result = service._board_for(t.id)
    assert result == "test-board"


def test_board_for_comment_returns_bound_board_when_ticket_id_given(service):
    """When ticket_id is provided, _board_for_comment delegates to
    _board_for and returns the ticket's board."""
    t = service.create("board-for-comment-test")
    # _board_for_comment with ticket_id → delegates to _board_for
    result = service._board_for_comment(1, ticket_id=t.id)
    assert result == "test-board"


# ---------------------------------------------------------------------------
# Cross-board migration
# ---------------------------------------------------------------------------


@pytest.fixture
def migrate_env(settings, service):
    """Register two boards (test-board + other-board) in the repos
    config singleton and init the target DB, so ``migrate`` can
    validate its target. Returns (service, other_service)."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.core import db as _db

    _cfg._repos_config = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="proj-a",
                langfuse_public_key="pk-a",
                langfuse_secret_key="sk-a",
            ),
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="proj-b",
                langfuse_public_key="pk-b",
                langfuse_secret_key="sk-b",
            ),
        }
    )
    _db.init_db(settings, board_id="other-board")
    other = TicketService(settings, board_id="other-board")
    yield service, other
    _cfg._repos_config = None


def test_migrate_moves_ticket_history_and_workspace(settings, migrate_env):
    service, other = migrate_env
    t = service.create("Misrouted fix", "fix belongs to the other repo")
    service.add_comment(t.id, "a human remark")
    src_ws = settings.workspaces_dir_for("test-board") / t.id

    migrated = service.migrate(t.id, "other-board", note="belongs there")

    assert migrated.board_id == "other-board"
    assert migrated.state is State.DRAFT
    # Row moved: gone from the source DB, present in the target DB.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as TicketModel

    with _db.session(settings, "test-board") as s:
        assert s.get(TicketModel, t.id) is None
    with _db.session(settings, "other-board") as s:
        assert s.get(TicketModel, t.id) is not None

    # Workspace moved with its description.
    dst_ws = settings.workspaces_dir_for("other-board") / t.id
    assert not src_ws.exists()
    assert migrated.workspace_path == str(dst_ws)
    assert other.workspace(migrated).read_description() == (
        "fix belongs to the other repo"
    )

    # History preserved + migration event appended, hash chain intact.
    hist = other.history(t.id)
    assert hist[0].note == "created"
    assert "migrated from board 'test-board' to 'other-board'" in hist[-1].note
    assert "belongs there" in hist[-1].note
    for prev, cur in zip(hist, hist[1:], strict=False):
        assert cur.prev_hash == prev.hash

    # Comments moved.
    comments = other.list_comments(t.id)
    assert [c.body for c in comments] == ["a human remark"]


def test_migrate_accepts_repo_id_and_resets_block_state(migrate_env):
    service, other = migrate_env
    t = service.create("Blocked elsewhere", "body")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="not actionable here")
    migrated = service.migrate(t.id, "other-repo")
    assert migrated.board_id == "other-board"
    assert migrated.state is State.DRAFT
    assert migrated.blocked_from is None
    assert "(was blocked)" in other.history(t.id)[-1].note


def test_migrate_prunes_repo_clone_and_baseline_cache(settings, migrate_env):
    service, _ = migrate_env
    t = service.create("With clone", "body")
    ws = service.workspace(t)
    (ws.repo_dir / ".git").mkdir(parents=True)
    (ws.artifacts_dir / "baseline_check.json").write_text("{}")
    (ws.artifacts_dir / "draft-original.md").write_text("keep me")

    service.migrate(t.id, "other-board")

    dst_ws = settings.workspaces_dir_for("other-board") / t.id
    assert not (dst_ws / "repo").exists()
    assert not (dst_ws / "artifacts" / "baseline_check.json").exists()
    assert (dst_ws / "artifacts" / "draft-original.md").read_text() == "keep me"


def test_migrate_rejects_bad_targets_and_states(migrate_env):
    service, _ = migrate_env
    t = service.create("t", "b")
    with pytest.raises(ValueError, match="unknown target board"):
        service.migrate(t.id, "no-such-board")
    with pytest.raises(ValueError, match="already on board"):
        service.migrate(t.id, "test-board")
    with pytest.raises(KeyError):
        service.migrate("nonexistent-id", "other-board")

    # Leaf epic (no children) CAN be migrated — the old hard-block is
    # lifted so that mis-filed epics can be moved.
    epic = service.create("an epic", kind=TicketKind.EPIC)
    migrated_epic = service.migrate(epic.id, "other-board")
    assert migrated_epic.board_id == "other-board"
    assert migrated_epic.state is State.DRAFT

    # A non-epic child linked to a parent KEEPS the parent link across the
    # migration — cross-board parent links are now supported, so the link
    # survives the move intact.
    parent = service.create("parent task")
    child = service.create("child task", parent_id=parent.id)
    migrated_child = service.migrate(child.id, "other-board")
    assert migrated_child.board_id == "other-board"
    assert migrated_child.parent_id == parent.id

    # A non-epic parent WITH children is still blocked (the subtree
    # path only triggers for kind == TicketKind.EPIC).
    parent2 = service.create("parent2 task")
    service.create("child2 task", parent_id=parent2.id)
    with pytest.raises(ValueError, match="has child tickets"):
        service.migrate(parent2.id, "other-board")

    # In-flight states refuse migration.
    busy = service.create("busy", "b")
    service.transition(busy.id, State.READY)
    service.transition(busy.id, State.DELIVERABLE)
    with pytest.raises(ValueError, match="can be migrated"):
        service.migrate(busy.id, "other-board")


# ---------------------------------------------------------------------------
# Epic subtree migration
# ---------------------------------------------------------------------------


def test_migrate_epic_subtree_moves_all_tickets(settings, migrate_env):
    """An epic with children and grandchildren migrates atomically:
    all tickets land on the target board with parent_id links intact,
    and none remain on the source board."""
    service, other = migrate_env

    # Build a 3-level tree: epic → child_a → grandchild
    #                           epic → child_b
    epic = service.create("Epic", kind=TicketKind.EPIC)
    child_a = service.create("Child A", parent_id=epic.id)
    child_b = service.create("Child B", parent_id=epic.id)
    grandchild = service.create("Grandchild", parent_id=child_a.id)

    # Add comments and history to some tickets so we verify preservation.
    service.add_comment(epic.id, "epic comment")
    service.add_comment(child_a.id, "child comment")

    # Workspace dirs — create marker files so we can verify the move.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        ws = settings.workspaces_dir_for("test-board") / tid
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "description.md").write_text(f"desc-{tid}")

    migrated = service.migrate(epic.id, "other-board", note="wrong board")

    # Root ticket returned.
    assert migrated.id == epic.id
    assert migrated.board_id == "other-board"
    assert migrated.state is State.DRAFT

    # All four tickets exist on the target board.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        t = other.get(tid)
        assert t is not None, f"{tid} missing from target board"
        assert t.board_id == "other-board"
        assert t.state is State.DRAFT

    # Parent links intact.
    assert other.get(child_a.id).parent_id == epic.id
    assert other.get(child_b.id).parent_id == epic.id
    assert other.get(grandchild.id).parent_id == child_a.id

    # All four tickets gone from source board.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as TicketModel

    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        with _db.session(settings, "test-board") as s:
            assert s.get(TicketModel, tid) is None, f"{tid} still on source board"

    # Workspace dirs moved to target board.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        src_ws = settings.workspaces_dir_for("test-board") / tid
        dst_ws = settings.workspaces_dir_for("other-board") / tid
        assert not src_ws.exists(), f"source workspace {tid} still exists"
        assert dst_ws.exists(), f"target workspace {tid} missing"
        assert (dst_ws / "description.md").read_text() == f"desc-{tid}"

    # History preserved: each ticket has at least a creation event and
    # a migration event.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        hist = other.history(tid)
        assert len(hist) >= 2, f"{tid}: expected ≥2 events, got {len(hist)}"
        assert hist[0].note == "created"
        assert "migrated from board 'test-board' to 'other-board'" in hist[-1].note

    # Comments preserved.
    assert [c.body for c in other.list_comments(epic.id)] == ["epic comment"]
    assert [c.body for c in other.list_comments(child_a.id)] == ["child comment"]
    assert other.list_comments(child_b.id) == []
    assert other.list_comments(grandchild.id) == []

    # Hash chain intact for each ticket.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        hist = other.history(tid)
        for prev, cur in zip(hist, hist[1:], strict=False):
            assert cur.prev_hash == prev.hash, f"{tid}: broken hash chain"


def test_migrate_epic_subtree_rejects_non_migratable_child(migrate_env):
    """A child in a non-migratable state blocks the entire subtree
    migration with a clear error naming the offending ticket."""
    service, _ = migrate_env

    epic = service.create("Epic", kind=TicketKind.EPIC)
    child_ok = service.create("OK child", parent_id=epic.id)
    child_bad = service.create("Bad child", parent_id=epic.id)
    # Transition child_bad to DELIVERABLE — not in _MIGRATABLE_STATES.
    service.transition(child_bad.id, State.READY)
    service.transition(child_bad.id, State.DELIVERABLE)

    with pytest.raises(ValueError, match="non-migratable states"):
        service.migrate(epic.id, "other-board")

    # The error should name the blocking child.
    with pytest.raises(ValueError, match=child_bad.id):
        service.migrate(epic.id, "other-board")

    # Verify nothing moved: the epic and children still on source board.
    assert service.get(epic.id) is not None
    assert service.get(child_ok.id) is not None
    assert service.get(child_bad.id) is not None


def test_migrate_epic_subtree_rolls_back_on_db_failure(settings, migrate_env):
    """When the target DB insert fails mid-subtree, workspace dirs are
    rolled back to the source board and the source DB is untouched."""
    service, other = migrate_env

    epic = service.create("Epic", kind=TicketKind.EPIC)
    child = service.create("Child", parent_id=epic.id)

    # Create workspace dirs with marker files.
    for tid in [epic.id, child.id]:
        ws = settings.workspaces_dir_for("test-board") / tid
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "description.md").write_text(f"desc-{tid}")

    # Pre-create a ticket with the same ID as the child on the target
    # board, so the insert of the child row will fail with an integrity
    # error — triggering the rollback path.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as TicketModel

    with _db.session(settings, "other-board") as s:
        s.add(
            TicketModel(
                id=child.id,
                title="collision",
                board_id="other-board",
                workspace_path=str(
                    settings.workspaces_dir_for("other-board") / child.id
                ),
            )
        )
        s.commit()

    import sqlalchemy.exc

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        service.migrate(epic.id, "other-board")

    # Workspace dirs rolled back: source still has them, target does not.
    for tid in [epic.id, child.id]:
        src_ws = settings.workspaces_dir_for("test-board") / tid
        dst_ws = settings.workspaces_dir_for("other-board") / tid
        assert src_ws.exists(), f"source workspace {tid} was not rolled back"
        assert (src_ws / "description.md").read_text() == f"desc-{tid}", (
            f"source workspace {tid} content lost"
        )
        # Target dirs should not exist (or be empty if created then rolled back).
        assert not dst_ws.exists() or not any(dst_ws.iterdir()), (
            f"target workspace {tid} not cleaned up"
        )

    # Source DB untouched: both tickets still present.
    assert service.get(epic.id) is not None
    assert service.get(child.id) is not None


# ---------------------------------------------------------------------------
# DB maintenance pass
# ---------------------------------------------------------------------------


class TestDbMaintenancePass:
    """Tests for periodic DB maintenance: event cap + archive purge +
    PRAGMA optimize + WAL checkpoint."""

    def test_empty_db_returns_zero_summary(self, service):
        """db_maintenance_pass on an empty DB returns all-zero summary
        and does not error."""
        summary = service.db_maintenance_pass()
        assert summary == {
            "archived_purged": 0,
            "events_pruned": 0,
            "comments_pruned": 0,
            "tickets_pruned": 0,
        }

    def test_event_cap_prunes_excess(self, service, settings):
        """After accumulating > max_events_per_ticket events on a
        non-terminal ticket, only the most recent max_events_per_ticket
        remain, and the earliest remaining event has prev_hash=None."""
        settings.max_events_per_ticket = 5
        t = service.create("event-cap test")

        # Insert 10 same-state events via add_step_event.
        for i in range(10):
            service.add_step_event(t.id, f"step {i}")

        # Verify 11 events before maintenance (1 created + 10 steps).
        assert len(service.history(t.id)) == 11

        summary = service.db_maintenance_pass()
        assert summary["events_pruned"] == 6  # 11 - 5 = 6
        assert summary["tickets_pruned"] == 1

        # After pruning, 5 events remain.
        hist = service.history(t.id)
        assert len(hist) == 5

        # The earliest remaining event must have prev_hash=None.
        assert hist[0].prev_hash is None

        # The ticket itself must still exist.
        assert service.get(t.id) is not None

    def test_max_events_zero_disables_event_cap(self, service, settings):
        """Setting max_events_per_ticket=0 disables per-ticket event
        capping."""
        settings.max_events_per_ticket = 0
        t = service.create("no-cap test")

        for i in range(50):
            service.add_step_event(t.id, f"step {i}")

        summary = service.db_maintenance_pass()
        assert summary["events_pruned"] == 0
        assert summary["tickets_pruned"] == 0
        assert len(service.history(t.id)) == 51  # all remain

    def test_archive_purge_runs_during_maintenance_pass(self, service, settings):
        """db_maintenance_pass calls _maybe_purge_archived, so terminal
        tickets beyond max_archived_tickets are purged even when no
        ticket has recently transitioned to a terminal state."""
        settings.max_archived_tickets = 2

        # Create 4 terminal tickets without triggering the per-transition
        # purge (we'll close them while cap is high, then lower it).
        settings.max_archived_tickets = 100
        tickets = []
        for i in range(4):
            t = service.create(f"purge test {i}")
            _close_ticket(service, t)
            tickets.append(t)

        # Now lower the cap and run maintenance.
        settings.max_archived_tickets = 2
        summary = service.db_maintenance_pass()

        assert summary["archived_purged"] == 2

        # Only 2 terminal tickets remain (the two newest).
        assert _terminal_count(service) == 2
        # Oldest two should be gone.
        assert service.get(tickets[0].id) is None
        assert service.get(tickets[1].id) is None
        assert service.get(tickets[2].id) is not None
        assert service.get(tickets[3].id) is not None

    def test_non_terminal_not_deleted(self, service, settings):
        """The event cap only prunes TicketEvent rows; it never deletes
        the Ticket row itself."""
        settings.max_events_per_ticket = 2
        t = service.create("keep-alive test")

        for i in range(10):
            service.add_step_event(t.id, f"step {i}")

        summary = service.db_maintenance_pass()
        assert summary["events_pruned"] > 0
        assert service.get(t.id) is not None
        assert service.get(t.id).state == State.DRAFT

    # --- comment cap tests ---

    def test_comment_cap_prunes_excess(self, service, settings):
        """After accumulating > max_comments_per_ticket comments on a
        non-terminal ticket, only the most recent max_comments_per_ticket
        remain (when all are unprotected)."""
        settings.max_comments_per_ticket = 5
        t = service.create("comment-cap test")

        # Add 10 comments (all closed so they're unprotected).
        for i in range(10):
            c = service.add_comment(t.id, f"comment {i}")
            # Close it via close_thread so it's unprotected.
            service.close_thread(c.id, ticket_id=t.id)

        # Verify 10 comments before maintenance.
        assert _comment_count(service, t.id) == 10

        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 5  # 10 - 5 = 5

        # After pruning, 5 comments remain.
        remaining = _comment_count(service, t.id)
        assert remaining == 5

        # The ticket itself must still exist.
        assert service.get(t.id) is not None

    def test_max_comments_zero_disables_comment_cap(self, service, settings):
        """Setting max_comments_per_ticket=0 disables per-ticket comment
        capping."""
        settings.max_comments_per_ticket = 0
        t = service.create("no-cap test")

        for i in range(50):
            c = service.add_comment(t.id, f"comment {i}")
            service.close_thread(c.id, ticket_id=t.id)

        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0
        assert _comment_count(service, t.id) == 50  # all remain

    def test_comment_cap_under_limit_noop(self, service, settings):
        """When a ticket has fewer comments than the cap, nothing is pruned."""
        settings.max_comments_per_ticket = 20
        t = service.create("under-cap test")

        c = service.add_comment(t.id, "only comment")
        service.close_thread(c.id, ticket_id=t.id)

        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0
        assert _comment_count(service, t.id) == 1

    def test_comment_cap_protects_open_thread(self, service, settings):
        """An open top-level thread (closed_at IS NULL) and its replies
        are never pruned, even when the ticket exceeds the cap."""
        from datetime import datetime, timezone

        from robotsix_mill.core import db
        from robotsix_mill.core.models import Comment

        settings.max_comments_per_ticket = 3
        t = service.create("open-thread test")

        # Create an open [ASK_USER] thread.
        ask = service.add_comment(t.id, "[ASK_USER] a question?", author="agent")
        # Add a reply to it.
        service.add_comment(t.id, "reply to ask", author="user", parent_id=ask.id)

        # Pile on many closed comments (oldest first).
        for i in range(10):
            c = service.add_comment(t.id, f"old closed {i}")
            # Close it so it's unprotected.
            with db.session(settings, service._board_for(t.id)) as s:
                cmt = s.get(Comment, c.id)
                cmt.closed_at = datetime.now(timezone.utc)
                s.add(cmt)
                s.commit()

        # Before maintenance: 1 ask + 1 reply + 10 closed = 12 comments.
        assert _comment_count(service, t.id) == 12

        summary = service.db_maintenance_pass()
        # Cap is 3, we have 12 total. The ask + reply (2 protected) +
        # up to 1 unprotected = effective cap is 3. So we delete oldest
        # closed until total <= cap or no more unprotected.
        # We can only delete 9 of 10 closed (need to leave 1 to reach cap
        # of 3 with the 2 protected). So 9 deleted.
        assert summary["comments_pruned"] == 9

        remaining = _comment_count(service, t.id)
        assert remaining == 3  # ask + reply + 1 most-recent closed

        # The open ask thread and its reply must survive.
        assert _get_comment(service, ask.id) is not None
        assert _get_comment(service, ask.id).closed_at is None  # still open

    def test_comment_cap_resets_orphaned_parent_id(self, service, settings):
        """When a parent comment is deleted, surviving replies have their
        parent_id reset to None."""
        from datetime import datetime, timezone

        from robotsix_mill.core import db
        from robotsix_mill.core.models import Comment

        settings.max_comments_per_ticket = 3
        t = service.create("orphan-reset test")

        # Create the parent FIRST (oldest unprotected).
        parent = service.add_comment(t.id, "closed thread")

        # Add 5 filler closed comments between the parent and its reply,
        # so the reply is the newest comment and survives the prune while
        # the parent (oldest unprotected) is deleted.
        filler_ids: list[int] = []
        for i in range(5):
            c = service.add_comment(t.id, f"filler {i}")
            filler_ids.append(c.id)

        # Create the reply LAST (newest), pointing at the old parent.
        reply = service.add_comment(t.id, "reply", parent_id=parent.id)

        # Close all comments so they're all unprotected.
        with db.session(settings, service._board_for(t.id)) as s:
            for cid in [parent.id, reply.id] + filler_ids:
                cmt = s.get(Comment, cid)
                cmt.closed_at = datetime.now(timezone.utc)
                s.add(cmt)
            s.commit()

        # 7 comments total (parent + 5 fillers + reply), cap=3.
        # 4 oldest unprotected pruned: parent + filler 0-2.
        # Reply (id=7) survives with parent deleted → parent_id → None.
        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 4

        # The reply must survive and its parent_id must be reset to None.
        r = _get_comment(service, reply.id)
        assert r is not None, "reply should survive the prune"
        assert r.parent_id is None, "orphaned reply must have parent_id reset"

    def test_comment_cap_empty_db(self, service, settings):
        """db_maintenance_pass on an empty DB returns zero comments_pruned."""
        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0

    def test_comment_cap_terminal_ticket_not_capped(self, service, settings):
        """Comments on terminal (archived) tickets are NOT capped by
        db_maintenance_pass — they are reclaimed by the archive purge."""
        settings.max_comments_per_ticket = 2
        t = service.create("terminal test")

        # Add 10 comments.
        for i in range(10):
            service.add_comment(t.id, f"comment {i}")

        # Close the ticket to a terminal state.
        _close_ticket(service, t)

        # Run maintenance. The ticket is terminal, so the comment cap
        # loop only looks at non-terminal tickets. Comments should survive.
        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0
        # All 10 comments remain (they'll be cascade-deleted when the
        # archive purge fires).
        assert _comment_count(service, t.id) == 10

    def test_pragma_optimize_runs(self, service, settings, monkeypatch):
        """db_maintenance_pass issues PRAGMA optimize after cleanup."""

        from sqlalchemy import Connection

        pragmas_seen = []

        # Spy on exec_driver_sql to capture PRAGMA optimize.
        _orig_eds = Connection.exec_driver_sql

        def _spy_eds(conn_self, statement, *args, **kwargs):
            try:
                stmt_str = str(statement)
                if "pragma" in stmt_str.lower():
                    pragmas_seen.append(stmt_str)
            except Exception:
                pass
            return _orig_eds(conn_self, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, "exec_driver_sql", _spy_eds)

        service.db_maintenance_pass()

        assert any("optimize" in s.lower() for s in pragmas_seen), (
            f"PRAGMA optimize was not observed; saw: {pragmas_seen}"
        )

    def test_wal_truncation_after_maintenance_pass(self, service, settings):
        """db_maintenance_pass issues PRAGMA wal_checkpoint(TRUNCATE)
        and the WAL file is truncated to near-zero bytes afterward."""
        from robotsix_mill.core import db as db_mod

        # Locate the DB path and derive the WAL path.
        db_path = db_mod._db_path(settings, service.board_id)
        wal_path = db_path.with_suffix(db_path.suffix + "-wal")

        # Create a ticket and generate enough writes to grow the WAL.
        t = service.create("wal-truncation test")
        for i in range(50):
            service.add_step_event(t.id, f"wal event {i}")

        # The WAL file should exist and be non-trivial.
        # (On first access with WAL mode, SQLite creates the WAL file.)
        if wal_path.exists():
            wal_size_before = wal_path.stat().st_size
        else:
            wal_size_before = 0

        # Run the maintenance pass, which now includes the TRUNCATE checkpoint.
        summary = service.db_maintenance_pass()

        # After the pass the WAL should be truncated to ~0 bytes or absent.
        if wal_path.exists():
            wal_size_after = wal_path.stat().st_size
        else:
            wal_size_after = 0

        # If the WAL was non-zero before, assert it shrank.  When the
        # file never existed (size 0 before and after) the test still
        # passes — the checkpoint call completed without error, and the
        # assertion only fails when a non-zero WAL fails to shrink.
        assert wal_size_after <= wal_size_before, (
            f"WAL file grew after TRUNCATE checkpoint: "
            f"{wal_size_before} → {wal_size_after}"
        )
        if wal_size_before > 0:
            assert wal_size_after < 100, (
                f"WAL file not truncated: {wal_size_after} bytes remain"
            )

        # Spot-check: the summary contract is unchanged.
        assert set(summary.keys()) == {
            "archived_purged",
            "events_pruned",
            "comments_pruned",
            "tickets_pruned",
        }


def test_close_tracker_from_blocked_clears_blocked_from(service):
    """close_tracker transitions a BLOCKED ticket to CLOSED with blocked_from cleared."""
    t = service.create("close tracker test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    assert reloaded.blocked_from is not None

    service.close_tracker(t.id)
    reloaded = service.get(t.id)
    assert reloaded.state is State.CLOSED
    assert reloaded.blocked_from is None


def test_close_tracker_raises_on_terminal_states(service):
    """close_tracker raises TransitionError when ticket is already terminal."""
    # DONE
    t = service.create("done task")
    service.transition(t.id, State.DONE)
    with pytest.raises(TransitionError):
        service.close_tracker(t.id)

    # CLOSED
    t2 = service.create("closed task")
    _close_ticket(service, t2)
    with pytest.raises(TransitionError):
        service.close_tracker(t2.id)

    # ANSWERED
    t3 = service.create("answered inquiry", kind=TicketKind.INQUIRY)
    _answer_ticket(service, t3)
    with pytest.raises(TransitionError):
        service.close_tracker(t3.id)

    # EPIC_CLOSED
    t4 = service.create("closed epic", kind=TicketKind.EPIC)
    _close_epic(service, t4)
    with pytest.raises(TransitionError):
        service.close_tracker(t4.id)


# --- resolve_by_suffix -------------------------------------------------


def test_resolve_by_suffix_exact_match(service):
    """resolve_by_suffix returns the full ID when exactly one ticket ends with suffix."""
    t = service.create("suffix test ticket")
    suffix = t.id[-4:]
    result = service.resolve_by_suffix(suffix)
    assert result == t.id


def test_resolve_by_suffix_no_match(service):
    """resolve_by_suffix returns None when no ticket ID ends with the suffix."""
    result = service.resolve_by_suffix("nonexistent-suffix-zzzz")
    assert result is None


def test_resolve_by_suffix_ambiguous(service):
    """resolve_by_suffix raises AmbiguousTicketId when multiple tickets share suffix."""
    from robotsix_mill.core import db as db_mod
    from robotsix_mill.core.models import Ticket

    # Create two tickets with known IDs that share the same 4-char suffix.
    shared_suffix = "abcd"
    id1 = f"20250101T000000Z-ticket-one-{shared_suffix}"
    id2 = f"20250101T000001Z-ticket-two-{shared_suffix}"

    with db_mod.session(service.settings, service.board_id) as s:
        for tid in (id1, id2):
            s.add(
                Ticket(
                    id=tid,
                    title="ambiguous test",
                    state=State.DRAFT,
                    kind=TicketKind.TASK,
                    source="user",
                    workspace_path="",
                )
            )
        s.commit()

    with pytest.raises(AmbiguousTicketId):
        service.resolve_by_suffix(shared_suffix)


def test_resolve_by_suffix_longer_than_four_chars(service):
    """resolve_by_suffix works with suffixes longer than 4 chars."""
    t = service.create("longer suffix test")
    suffix = t.id[-8:]  # 8 chars
    result = service.resolve_by_suffix(suffix)
    assert result == t.id


def test_resolve_by_suffix_exact_full_id(service):
    """resolve_by_suffix works when the suffix is the full ticket ID."""
    t = service.create("full id suffix test")
    result = service.resolve_by_suffix(t.id)
    assert result == t.id


# --- DiagnosticEvent tests ---


def test_emit_diagnostic_event_creates_row(service):
    """emit_diagnostic_event creates a DiagnosticEvent row."""
    t = service.create("emit test")
    event = service.emit_diagnostic_event(
        ticket_id=t.id,
        category="CI_FAILURE",
        sub_category="lint",
        reason="flake8 failed",
    )
    assert event is not None
    assert event.ticket_id == t.id
    assert event.category == "CI_FAILURE"
    assert event.sub_category == "lint"
    assert event.reason == "flake8 failed"
    assert event.repo_id == "test-board"


def test_emit_diagnostic_event_dedup_same_ticket_and_sub_category(service):
    """Second emit with same ticket_id + sub_category is skipped (dedup)."""
    t = service.create("dedup test")
    first = service.emit_diagnostic_event(
        ticket_id=t.id,
        category="CI_FAILURE",
        sub_category="lint",
        reason="flake8 failed",
    )
    assert first is not None
    second = service.emit_diagnostic_event(
        ticket_id=t.id,
        category="CI_FAILURE",
        sub_category="lint",
        reason="flake8 failed again",  # different reason but same sub_category
    )
    assert second is None


def test_emit_diagnostic_event_different_sub_category_allowed(service):
    """Different sub_category on the same ticket creates a second event."""
    t = service.create("multi sub test")
    e1 = service.emit_diagnostic_event(
        ticket_id=t.id,
        category="CI_FAILURE",
        sub_category="lint",
        reason="flake8",
    )
    assert e1 is not None
    e2 = service.emit_diagnostic_event(
        ticket_id=t.id,
        category="CI_FAILURE",
        sub_category="type-check",
        reason="mypy",
    )
    assert e2 is not None
    assert e1.id != e2.id


def test_list_diagnostic_events_filter_by_category(service):
    """list_diagnostic_events filters by category."""
    t = service.create("list test")
    service.emit_diagnostic_event(
        ticket_id=t.id,
        category="CI_FAILURE",
        sub_category="lint",
        reason="flake8",
    )
    service.emit_diagnostic_event(
        ticket_id=t.id,
        category="OTHER",
        sub_category="foo",
        reason="bar",
    )
    ci_events = service.list_diagnostic_events(category="CI_FAILURE")
    assert len(ci_events) == 1
    assert ci_events[0].category == "CI_FAILURE"


def test_list_diagnostic_events_filter_by_ticket_id(service):
    """list_diagnostic_events filters by ticket_id."""
    t1 = service.create("t1")
    t2 = service.create("t2")
    service.emit_diagnostic_event(
        ticket_id=t1.id, category="CI_FAILURE", sub_category="a", reason="r1"
    )
    service.emit_diagnostic_event(
        ticket_id=t2.id, category="CI_FAILURE", sub_category="b", reason="r2"
    )
    assert len(service.list_diagnostic_events(ticket_id=t1.id)) == 1
    assert len(service.list_diagnostic_events(ticket_id=t2.id)) == 1


def test_list_diagnostic_events_filter_by_sub_category(service):
    """list_diagnostic_events filters by sub_category."""
    t = service.create("sub filter test")
    service.emit_diagnostic_event(
        ticket_id=t.id, category="CI_FAILURE", sub_category="lint", reason="r1"
    )
    service.emit_diagnostic_event(
        ticket_id=t.id, category="CI_FAILURE", sub_category="type", reason="r2"
    )
    assert len(service.list_diagnostic_events(sub_category="lint")) == 1
    assert len(service.list_diagnostic_events(sub_category="type")) == 1


def test_check_recurring_categories_below_threshold(service):
    """check_recurring_categories returns empty when no group crosses threshold."""
    t = service.create("below threshold")
    service.emit_diagnostic_event(
        ticket_id=t.id, category="CI_FAILURE", sub_category="lint", reason="r"
    )
    groups = service.check_recurring_categories("CI_FAILURE", threshold=2)
    assert groups == []


def test_check_recurring_categories_above_threshold(service):
    """check_recurring_categories returns groups at or above threshold."""
    t1 = service.create("above t1")
    t2 = service.create("above t2")
    t3 = service.create("above t3")
    for t_obj in (t1, t2, t3):
        service.emit_diagnostic_event(
            ticket_id=t_obj.id,
            category="CI_FAILURE",
            sub_category="lint",
            reason="flake8",
        )
    groups = service.check_recurring_categories("CI_FAILURE", threshold=2)
    assert len(groups) == 1  # lint group
    assert groups[0]["distinct_tickets"] == 3
    assert groups[0]["sub_category"] == "lint"


def test_count_distinct_tickets_for_category(service):
    """count_distinct_tickets_for_category returns correct count."""
    t1 = service.create("count t1")
    t2 = service.create("count t2")
    for t_obj in (t1, t2):
        service.emit_diagnostic_event(
            ticket_id=t_obj.id,
            category="CI_FAILURE",
            sub_category="lint",
            reason="r",
        )
    assert service.count_distinct_tickets_for_category("CI_FAILURE", "lint") == 2
    assert service.count_distinct_tickets_for_category("CI_FAILURE", "nonexistent") == 0
