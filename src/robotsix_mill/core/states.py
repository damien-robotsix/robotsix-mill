"""The pipeline state machine.

Each *active* state is owned by exactly one stage, which consumes
tickets in that state and transitions them onward. ``FAILED`` and
``BLOCKED`` are side states reachable from any active state when a stage
errors or escalates; they require human attention before re-entering the
pipeline.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    DRAFT = "draft"            # raw idea, awaiting refinement
    READY = "ready"           # actionable; awaiting implementation
    IN_REVIEW = "in_review"   # implemented; awaiting quality review
    DELIVERABLE = "deliverable"  # passed review; awaiting MR delivery
    DONE = "done"             # delivered (MR opened)
    FAILED = "failed"         # a stage hit an unrecoverable error
    BLOCKED = "blocked"       # escalated; needs a human


#: state -> the set of states it may transition to (the "happy path"
#: plus the always-available escalation edges).
TRANSITIONS: dict[State, set[State]] = {
    State.DRAFT: {State.READY, State.FAILED, State.BLOCKED},
    # implement currently routes straight to deliverable (review stage
    # not built yet); IN_REVIEW kept for when review lands.
    State.READY: {
        State.DELIVERABLE, State.IN_REVIEW, State.FAILED, State.BLOCKED,
    },
    # review can pass forward or bounce back for changes
    State.IN_REVIEW: {State.DELIVERABLE, State.READY, State.FAILED, State.BLOCKED},
    State.DELIVERABLE: {State.DONE, State.FAILED, State.BLOCKED},
    State.DONE: set(),
    # a human moves these back into the pipeline manually
    State.FAILED: {State.READY, State.DRAFT},
    State.BLOCKED: {State.READY, State.DRAFT},
}

#: active state -> name of the stage that consumes it.
STAGE_FOR_STATE: dict[State, str] = {
    State.DRAFT: "refine",
    State.READY: "implement",
    State.IN_REVIEW: "review",
    State.DELIVERABLE: "deliver",
}


def can_transition(src: State, dst: State) -> bool:
    return dst in TRANSITIONS.get(src, set())
