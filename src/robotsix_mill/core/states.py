"""The pipeline state machine.

Each *active* state is owned by exactly one stage, which consumes
tickets in that state and transitions them onward. ``ERRORED`` and
``BLOCKED`` are side states reachable from any active state when a stage
errors or escalates; they require human attention before re-entering the
pipeline. ``AWAITING_APPROVAL`` is a human-wait state between refine and
implement — it has no stage owner and pauses the chain until a human
approves.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    DRAFT = "draft"            # raw idea, awaiting refinement
    AWAITING_APPROVAL = "awaiting_approval"  # refined; awaiting human approval
    READY = "ready"           # actionable; awaiting implementation
    DELIVERABLE = "deliverable"  # implemented; awaiting MR delivery
    IN_REVIEW = "in_review"   # PR/MR open; awaiting human merge
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
    # PR open: merge stage polls -> merged=done, closed-unmerged=blocked
    State.IN_REVIEW: {State.DONE, State.ERRORED, State.BLOCKED},
    # done = merged: retrospect analyses it -> reviewed
    State.DONE: {State.CLOSED, State.ERRORED, State.BLOCKED},
    State.CLOSED: set(),
    # a human moves these back into the pipeline manually
    State.ERRORED: {State.READY, State.DRAFT},
    State.BLOCKED: {State.READY, State.DRAFT},
}

#: active state -> name of the stage that consumes it.
STAGE_FOR_STATE: dict[State, str] = {
    State.DRAFT: "refine",
    State.READY: "implement",
    State.DELIVERABLE: "deliver",
    State.IN_REVIEW: "merge",
    State.DONE: "retrospect",
}


def can_transition(src: State, dst: State) -> bool:
    return dst in TRANSITIONS.get(src, set())
