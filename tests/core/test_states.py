"""Exhaustive state-machine tests — pure functions, no DB, no TicketService."""

from __future__ import annotations


from robotsix_mill.core.states import (
    STAGE_FOR_STATE,
    TRANSITIONS,
    State,
    can_transition,
)

# Known stage names that STAGE_FOR_STATE values must belong to.
VALID_STAGE_NAMES = {
    "refine",
    "implement",
    "document",
    "review",
    "deliver",
    "merge",
    "ci_fix",
    "retrospect",
    "answer",
}

# States that should NOT appear as keys in STAGE_FOR_STATE.
NON_STAGE_STATES = {
    State.CLOSED,
    State.ERRORED,
    State.BLOCKED,
    State.HUMAN_ISSUE_APPROVAL,
    State.ANSWERED,
    State.EPIC_OPEN,
    State.EPIC_CLOSED,
    State.AWAITING_USER_REPLY,
}

# All states for iteration.
ALL_STATES = list(State)


# ---------------------------------------------------------------------------
# Every valid transition
# ---------------------------------------------------------------------------


def test_every_declared_transition_is_valid():
    """For every (src, dst) in TRANSITIONS: can_transition returns True."""
    for src, dsts in TRANSITIONS.items():
        for dst in dsts:
            assert can_transition(src, dst) is True, (
                f"TRANSITIONS declares {src.value} → {dst.value} but "
                f"can_transition returned False"
            )


# ---------------------------------------------------------------------------
# Every invalid transition
# ---------------------------------------------------------------------------


def test_every_undeclared_transition_is_invalid():
    """For every (src, dst) NOT in TRANSITIONS (excluding BLOCKED dynamic
    resume): can_transition returns False."""
    for src in ALL_STATES:
        allowed = TRANSITIONS.get(src, set())
        for dst in ALL_STATES:
            # Skip pairs explicitly declared in TRANSITIONS.
            if dst in allowed:
                continue
            # BLOCKED has dynamic resume via blocked_from — test that
            # separately, not here.
            if src is State.BLOCKED:
                continue

            assert can_transition(src, dst) is False, (
                f"{src.value} → {dst.value} is not declared but "
                f"can_transition returned True"
            )


# ---------------------------------------------------------------------------
# BLOCKED dynamic resume
# ---------------------------------------------------------------------------


def test_blocked_resume_to_blocked_from():
    """can_transition(BLOCKED, dst, blocked_from=dst) is True for every
    non-terminal state dst."""
    terminal = {State.CLOSED}
    for dst in ALL_STATES:
        if dst in terminal:
            continue
        assert can_transition(State.BLOCKED, dst, blocked_from=dst) is True, (
            f"BLOCKED → {dst.value} with blocked_from={dst.value} should be allowed"
        )


def test_blocked_without_blocked_from_fails_for_non_declared():
    """Without blocked_from, BLOCKED → non-declared dst must be False."""
    declared = TRANSITIONS.get(State.BLOCKED, set())
    for dst in ALL_STATES:
        if dst in declared:
            continue
        assert can_transition(State.BLOCKED, dst) is False, (
            f"BLOCKED → {dst.value} without blocked_from should be False"
        )


# ---------------------------------------------------------------------------
# CLOSED is terminal
# ---------------------------------------------------------------------------


def test_closed_is_terminal():
    """From CLOSED or ANSWERED, no transition to any state is allowed."""
    for dst in ALL_STATES:
        assert can_transition(State.CLOSED, dst) is False, (
            f"CLOSED → {dst.value} should be forbidden"
        )
        assert can_transition(State.ANSWERED, dst) is False, (
            f"ANSWERED → {dst.value} should be forbidden"
        )


# ---------------------------------------------------------------------------
# STAGE_FOR_STATE completeness
# ---------------------------------------------------------------------------


def test_stage_for_state_keys():
    """Every State that is NOT CLOSED/ERRORED/BLOCKED/HUMAN_ISSUE_APPROVAL
    must appear as a key in STAGE_FOR_STATE."""
    for state in ALL_STATES:
        if state in NON_STAGE_STATES:
            assert state not in STAGE_FOR_STATE, (
                f"{state.value} should NOT be in STAGE_FOR_STATE"
            )
        else:
            assert state in STAGE_FOR_STATE, (
                f"{state.value} must be a key in STAGE_FOR_STATE"
            )


def test_stage_for_state_values():
    """Each mapping value must be one of the known stage names."""
    for state, stage_name in STAGE_FOR_STATE.items():
        assert stage_name in VALID_STAGE_NAMES, (
            f"STAGE_FOR_STATE[{state.value}] = {stage_name!r} is not a valid stage name"
        )


# ---------------------------------------------------------------------------
# Inquiry edge-case spot-checks
# ---------------------------------------------------------------------------


def test_asked_to_answered():
    assert can_transition(State.ASKED, State.ANSWERED) is True


def test_asked_to_blocked():
    assert can_transition(State.ASKED, State.BLOCKED) is True


def test_asked_to_errored():
    assert can_transition(State.ASKED, State.ERRORED) is True


def test_blocked_resume_to_asked():
    assert can_transition(State.BLOCKED, State.ASKED, blocked_from=State.ASKED) is True


def test_stage_for_state_asked():
    assert STAGE_FOR_STATE[State.ASKED] == "answer"


def test_answered_not_in_stage_for_state():
    assert State.ANSWERED not in STAGE_FOR_STATE


def test_draft_to_human_issue_approval():
    assert can_transition(State.DRAFT, State.HUMAN_ISSUE_APPROVAL) is True


# --- IMPLEMENT_COMPLETE gate-check state ---


def test_implement_complete_to_human_mr_approval():
    """IMPLEMENT_COMPLETE → HUMAN_MR_APPROVAL is valid (gates passed)."""
    assert can_transition(State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL) is True


def test_implement_complete_to_fixing_ci():
    """IMPLEMENT_COMPLETE → FIXING_CI is valid (CI failing)."""
    assert can_transition(State.IMPLEMENT_COMPLETE, State.FIXING_CI) is True


def test_implement_complete_to_rebasing():
    """IMPLEMENT_COMPLETE → REBASING is valid (PR conflicting)."""
    assert can_transition(State.IMPLEMENT_COMPLETE, State.REBASING) is True


def test_implement_complete_to_done():
    """IMPLEMENT_COMPLETE → DONE is valid (PR merged while polling)."""
    assert can_transition(State.IMPLEMENT_COMPLETE, State.DONE) is True


def test_implement_complete_to_blocked():
    """IMPLEMENT_COMPLETE → BLOCKED is valid."""
    assert can_transition(State.IMPLEMENT_COMPLETE, State.BLOCKED) is True


def test_implement_complete_to_errored():
    """IMPLEMENT_COMPLETE → ERRORED is valid."""
    assert can_transition(State.IMPLEMENT_COMPLETE, State.ERRORED) is True


def test_implement_complete_to_waiting_auto_merge():
    """IMPLEMENT_COMPLETE → WAITING_AUTO_MERGE is valid."""
    assert can_transition(State.IMPLEMENT_COMPLETE, State.WAITING_AUTO_MERGE) is True


def test_implement_complete_stage_for_state():
    """IMPLEMENT_COMPLETE is consumed by the merge stage."""
    assert STAGE_FOR_STATE[State.IMPLEMENT_COMPLETE] == "merge"


# --- MAINTENANCE state ---


# --- HUMAN_MR_APPROVAL transitions (updated for silent fallback) ---


def test_human_mr_approval_to_implement_complete():
    """HUMAN_MR_APPROVAL → IMPLEMENT_COMPLETE is valid (silent fallback)."""
    assert can_transition(State.HUMAN_MR_APPROVAL, State.IMPLEMENT_COMPLETE) is True


def test_human_mr_approval_to_rebasing_removed():
    """HUMAN_MR_APPROVAL → REBASING is INVALID (removed — falls back via IMPLEMENT_COMPLETE)."""
    assert can_transition(State.HUMAN_MR_APPROVAL, State.REBASING) is False


def test_human_mr_approval_to_fixing_ci_removed():
    """HUMAN_MR_APPROVAL → FIXING_CI is INVALID (removed — falls back via IMPLEMENT_COMPLETE)."""
    assert can_transition(State.HUMAN_MR_APPROVAL, State.FIXING_CI) is False


# --- REBASING transitions (now go to IMPLEMENT_COMPLETE) ---


def test_rebasing_to_implement_complete():
    """REBASING → IMPLEMENT_COMPLETE is valid (rebase success → re-verify gates)."""
    assert can_transition(State.REBASING, State.IMPLEMENT_COMPLETE) is True


def test_rebasing_to_human_mr_approval_removed():
    """REBASING → HUMAN_MR_APPROVAL is INVALID (removed — goes via IMPLEMENT_COMPLETE)."""
    assert can_transition(State.REBASING, State.HUMAN_MR_APPROVAL) is False


# --- FIXING_CI transitions (now go to IMPLEMENT_COMPLETE) ---


def test_fixing_ci_to_implement_complete():
    """FIXING_CI → IMPLEMENT_COMPLETE is valid (ci fix success → re-verify gates)."""
    assert can_transition(State.FIXING_CI, State.IMPLEMENT_COMPLETE) is True


def test_fixing_ci_to_human_mr_approval_removed():
    """FIXING_CI → HUMAN_MR_APPROVAL is INVALID (removed — goes via IMPLEMENT_COMPLETE)."""
    assert can_transition(State.FIXING_CI, State.HUMAN_MR_APPROVAL) is False


def test_fixing_ci_to_blocked():
    assert can_transition(State.FIXING_CI, State.BLOCKED) is True


# --- DELIVERABLE transitions (now go to IMPLEMENT_COMPLETE) ---


def test_deliverable_to_implement_complete():
    """DELIVERABLE → IMPLEMENT_COMPLETE is valid (PR opened, gates not yet checked)."""
    assert can_transition(State.DELIVERABLE, State.IMPLEMENT_COMPLETE) is True


def test_deliverable_to_human_mr_approval_removed():
    """DELIVERABLE → HUMAN_MR_APPROVAL is INVALID (removed — goes via IMPLEMENT_COMPLETE)."""
    assert can_transition(State.DELIVERABLE, State.HUMAN_MR_APPROVAL) is False


# --- WAITING_AUTO_MERGE transitions (now use IMPLEMENT_COMPLETE fallback) ---


def test_waiting_auto_merge_to_implement_complete():
    """WAITING_AUTO_MERGE → IMPLEMENT_COMPLETE is valid (CI failure or conflict → gate-check)."""
    assert can_transition(State.WAITING_AUTO_MERGE, State.IMPLEMENT_COMPLETE) is True


def test_waiting_auto_merge_to_fixing_ci_removed():
    """WAITING_AUTO_MERGE → FIXING_CI is INVALID (falls back via IMPLEMENT_COMPLETE)."""
    assert can_transition(State.WAITING_AUTO_MERGE, State.FIXING_CI) is False


def test_waiting_auto_merge_to_rebasing_removed():
    """WAITING_AUTO_MERGE → REBASING is INVALID (falls back via IMPLEMENT_COMPLETE)."""
    assert can_transition(State.WAITING_AUTO_MERGE, State.REBASING) is False


def test_errored_as_destination():
    """States that declare ERRORED in TRANSITIONS can reach it."""
    for src in (
        State.DRAFT,
        State.HUMAN_ISSUE_APPROVAL,
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.REBASING,
        State.FIXING_CI,
        State.DONE,
        State.ASKED,
    ):
        assert can_transition(src, State.ERRORED) is True, (
            f"{src.value} → ERRORED should be valid"
        )


# ---------------------------------------------------------------------------
# DOCUMENTING + CODE_REVIEW spot-checks
# ---------------------------------------------------------------------------


def test_ready_to_documenting():
    assert can_transition(State.READY, State.DOCUMENTING) is True


def test_documenting_not_to_code_review():
    """Pipeline flip: doc no longer routes back into review."""
    assert can_transition(State.DOCUMENTING, State.CODE_REVIEW) is False


def test_ready_to_code_review():
    assert can_transition(State.READY, State.CODE_REVIEW) is True


def test_code_review_to_documenting():
    assert can_transition(State.CODE_REVIEW, State.DOCUMENTING) is True


def test_documenting_to_deliverable():
    assert can_transition(State.DOCUMENTING, State.DELIVERABLE) is True


def test_stage_for_state_documenting():
    assert STAGE_FOR_STATE[State.DOCUMENTING] == "document"


def test_code_review_not_to_deliverable():
    """Pipeline flip: review now routes to documenting (then deliverable),
    not directly to deliverable."""
    assert can_transition(State.CODE_REVIEW, State.DELIVERABLE) is False


def test_code_review_to_ready():
    assert can_transition(State.CODE_REVIEW, State.READY) is True


def test_code_review_to_blocked():
    assert can_transition(State.CODE_REVIEW, State.BLOCKED) is True


def test_code_review_to_errored():
    assert can_transition(State.CODE_REVIEW, State.ERRORED) is True


def test_stage_for_state_code_review():
    assert STAGE_FOR_STATE[State.CODE_REVIEW] == "review"


def test_code_review_not_undeclared_source():
    """Transitions FROM CODE_REVIEW to undeclared states are False."""
    for dst in (
        State.HUMAN_MR_APPROVAL,
        State.DONE,
        State.HUMAN_ISSUE_APPROVAL,
        State.DRAFT,
    ):
        assert can_transition(State.CODE_REVIEW, dst) is False, (
            f"CODE_REVIEW → {dst.value} should be invalid"
        )


def test_blocked_resume_to_code_review():
    assert (
        can_transition(State.BLOCKED, State.CODE_REVIEW, blocked_from=State.CODE_REVIEW)
        is True
    )


# ---------------------------------------------------------------------------
# AWAITING_USER_REPLY
# ---------------------------------------------------------------------------


def test_awaiting_user_reply_not_in_stage_for_state():
    """AWAITING_USER_REPLY must NOT be a key in STAGE_FOR_STATE — there
    is no stage for it."""
    assert State.AWAITING_USER_REPLY not in STAGE_FOR_STATE


def test_awaiting_user_reply_to_errored():
    assert can_transition(State.AWAITING_USER_REPLY, State.ERRORED) is True


def test_awaiting_user_reply_to_blocked():
    assert can_transition(State.AWAITING_USER_REPLY, State.BLOCKED) is True


def test_awaiting_user_reply_resume_via_paused_from():
    """Resume path: AWAITING_USER_REPLY → originating state when
    paused_from matches."""
    assert (
        can_transition(
            State.AWAITING_USER_REPLY,
            State.READY,
            paused_from=State.READY,
        )
        is True
    )


def test_awaiting_user_reply_no_paused_from_rejects():
    """Without paused_from, AWAITING_USER_REPLY → arbitrary state is
    rejected."""
    assert can_transition(State.AWAITING_USER_REPLY, State.READY) is False


def test_awaiting_user_reply_wrong_paused_from_rejects():
    """paused_from must match dst EXACTLY."""
    assert (
        can_transition(
            State.AWAITING_USER_REPLY,
            State.DELIVERABLE,
            paused_from=State.READY,
        )
        is False
    )


def test_every_stage_for_state_key_can_reach_awaiting_user_reply():
    """Every state with an automated stage can transition to
    AWAITING_USER_REPLY (any running stage can pause for a question)."""
    for src in STAGE_FOR_STATE:
        assert can_transition(src, State.AWAITING_USER_REPLY) is True, (
            f"{src.value} → AWAITING_USER_REPLY should be valid"
        )


def test_awaiting_user_reply_not_reachable_from_non_stage_states():
    """States without automated stages cannot transition to
    AWAITING_USER_REPLY."""
    for src in NON_STAGE_STATES:
        assert can_transition(src, State.AWAITING_USER_REPLY) is False, (
            f"{src.value} → AWAITING_USER_REPLY should be invalid"
        )


# ---------------------------------------------------------------------------
# Epic transition spot-checks
# ---------------------------------------------------------------------------


def test_epic_open_to_epic_closed():
    assert can_transition(State.EPIC_OPEN, State.EPIC_CLOSED) is True


def test_epic_open_to_blocked():
    assert can_transition(State.EPIC_OPEN, State.BLOCKED) is True


def test_epic_closed_is_terminal():
    assert can_transition(State.EPIC_CLOSED, State.EPIC_OPEN) is False


def test_epic_open_not_in_stage_for_state():
    assert State.EPIC_OPEN not in STAGE_FOR_STATE
