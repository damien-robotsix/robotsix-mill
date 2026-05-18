"""The pipeline state machine.

Each *active* state is owned by exactly one stage, which consumes
tickets in that state and transitions them onward. ``ERRORED`` and
``BLOCKED`` are side states reachable from any active state when a stage
errors or escalates; they require human attention before re-entering the
pipeline. ``AWAITING_APPROVAL`` is a human-wait state between refine and
implement — it has no stage owner and pauses the chain until a human
approves. ``REBASING`` is an active state between ``IN_REVIEW`` and
``IN_REVIEW``: the merge stage detects a conflicting PR and transitions
to ``REBASING``, then on the next poll runs the rebase agent and
force-pushes the ticket branch. On success it returns to ``IN_REVIEW``;
on temporary failure it stays in ``REBASING`` for a retry; on exhaustion
it escalates to ``BLOCKED``.

When a ticket is blocked, the state it was blocked *from* is recorded
(``Ticket.blocked_from``). A human can **resume** the blocked ticket
straight back to that originating state to re-run only the failed stage
(no need to replay earlier stages). The existing ``BLOCKED → READY`` and
``BLOCKED → DRAFT`` transitions remain available as explicit human
overrides that re-run the full downstream chain.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    DRAFT = "draft"            # raw idea, awaiting refinement
    AWAITING_APPROVAL = "awaiting_approval"  # refined; awaiting human approval
    READY = "ready"           # actionable; awaiting implementation
    DELIVERABLE = "deliverable"  # implemented; awaiting MR delivery
    IN_REVIEW = "in_review"   # PR/MR open; awaiting human merge
    REBASING = "rebasing"     # conflicting PR; rebase agent in progress
    DONE = "done"             # PR/MR merged; awaiting retrospect
    CLOSED = "closed"        # retrospected; pipeline complete (terminal)
    ERRORED = "errored"       # a stage threw an unhandled exception
    BLOCKED = "blocked"       # escalated; needs a human


#: state -> the set of states it may transition to (the "happy path"
#: plus the always-available escalation edges).
TRANSITIONS: dict[State, set[State]] = {
    State.DRAFT: {State.READY, State.AWAITING_APPROVAL, State.ERRORED, State.BLOCKED},
    # awaiting_approval is a human-wait state; the human approves → ready
    # or escalates → blocked/failed.
    State.AWAITING_APPROVAL: {State.READY, State.ERRORED, State.BLOCKED},
    # implement routes straight to deliverable (the PR itself is the
    # review — no separate pre-deliver code-review state).
    State.READY: {State.DELIVERABLE, State.ERRORED, State.BLOCKED},
    State.DELIVERABLE: {State.IN_REVIEW, State.ERRORED, State.BLOCKED},
    # PR open: merge stage polls -> merged=done, closed-unmerged=blocked,
    # conflicting=rebasing (auto-rebase cycle).
    State.IN_REVIEW: {State.DONE, State.REBASING, State.ERRORED, State.BLOCKED},
    # rebasing: merge stage runs rebase agent -> back to in_review on
    # success, retry on failure, block on exhaustion.
    State.REBASING: {State.IN_REVIEW, State.ERRORED, State.BLOCKED},
    # done = merged: retrospect analyses it -> reviewed
    State.DONE: {State.CLOSED, State.ERRORED, State.BLOCKED},
    State.CLOSED: set(),
    # a human moves these back into the pipeline manually
    State.ERRORED: {State.READY, State.DRAFT},
    # BLOCKED: human can override to READY or DRAFT (re-run downstream),
    # or resume to the originating state (re-run only the failed stage).
    State.BLOCKED: {State.READY, State.DRAFT},
}

#: active state -> name of the stage that consumes it.
STAGE_FOR_STATE: dict[State, str] = {
    State.DRAFT: "refine",
    State.READY: "implement",
    State.DELIVERABLE: "deliver",
    State.IN_REVIEW: "merge",
    State.REBASING: "merge",
    State.DONE: "retrospect",
}


def can_transition(src: State, dst: State, blocked_from: State | None = None) -> bool:
    """Return True if ``src → dst`` is a legal transition.

    When *src* is ``BLOCKED``, *dst* is also allowed when it matches
    the recorded ``blocked_from`` state (the resume path).  The
    existing ``BLOCKED → READY`` and ``BLOCKED → DRAFT`` overrides are
    always available regardless of ``blocked_from``.
    """
    allowed = TRANSITIONS.get(src, set())
    if dst in allowed:
        return True
    if src is State.BLOCKED and blocked_from is not None and dst is blocked_from:
        return True
    return False
