"""The pipeline state machine.

Each *active* state is owned by exactly one stage, which consumes
tickets in that state and transitions them onward. ``ERRORED`` and
``BLOCKED`` are side states reachable from any active state when a stage
errors or escalates; they require human attention before re-entering the
pipeline. ``HUMAN_ISSUE_APPROVAL`` is a human-wait state between refine
and implement — it has no stage owner and pauses the chain until a human
approves. ``WAITING_AUTO_MERGE`` is an active state between
``HUMAN_MR_APPROVAL`` and ``DONE``: the merge stage detects a mergeable PR
whose CI is pending and auto-merge is eligible, then on each poll
re-checks CI; when CI goes green it auto-merges to ``DONE``, on CI
failure it transitions to ``FIXING_CI``, and on eligibility changes it
returns to ``HUMAN_MR_APPROVAL``. ``REBASING`` is an active state between
``HUMAN_MR_APPROVAL`` and ``HUMAN_MR_APPROVAL``: the merge stage detects a
conflicting PR and transitions to ``REBASING``, then on the next poll
runs the rebase agent and force-pushes the ticket branch. On success it
returns to ``HUMAN_MR_APPROVAL``; on temporary failure it stays in
``REBASING`` for a retry; on exhaustion it escalates to ``BLOCKED``.
``FIXING_CI`` is an active state between ``HUMAN_MR_APPROVAL`` and
``HUMAN_MR_APPROVAL``: the merge stage detects a mergeable PR with
failing CI and transitions to ``FIXING_CI``, then on the next poll runs
the CI-fix agent. On success it returns to ``HUMAN_MR_APPROVAL``; on
failure it escalates to ``BLOCKED``.

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
    HUMAN_ISSUE_APPROVAL = "human_issue_approval"  # refined; awaiting human approval
    READY = "ready"           # actionable; awaiting implementation
    DOCUMENTING = "documenting"  # implemented; documentation agent updating docs
    CODE_REVIEW = "code_review"  # documented; awaiting automated code review
    DELIVERABLE = "deliverable"  # reviewed; awaiting MR delivery
    HUMAN_MR_APPROVAL = "human_mr_approval"   # PR/MR open; awaiting human merge
    WAITING_AUTO_MERGE = "waiting_auto_merge"  # PR open + CI pending; auto-merge when green
    REBASING = "rebasing"     # conflicting PR; rebase agent in progress
    FIXING_CI = "fixing_ci"   # PR open + failing CI; auto-fix in progress
    DONE = "done"             # PR/MR merged; awaiting retrospect
    CLOSED = "closed"        # retrospected; pipeline complete (terminal)
    ERRORED = "errored"       # a stage threw an unhandled exception
    BLOCKED = "blocked"       # escalated; needs a human
    ASKED = "asked"           # inquiry submitted; awaiting answer
    ANSWERED = "answered"     # inquiry answered (terminal)
    EPIC_OPEN = "epic_open"   # epic actively collecting/grouping children
    EPIC_CLOSED = "epic_closed"  # epic closed (terminal)


#: state -> the set of states it may transition to (the "happy path"
#: plus the always-available escalation edges).
TRANSITIONS: dict[State, set[State]] = {
    State.DRAFT: {State.READY, State.HUMAN_ISSUE_APPROVAL, State.ERRORED, State.BLOCKED, State.CLOSED, State.DONE},
    # human_issue_approval is a human-wait state; the human approves → ready,
    # rejects back to draft with comments, or escalates → blocked/failed.
    State.HUMAN_ISSUE_APPROVAL: {State.READY, State.DRAFT, State.ERRORED, State.BLOCKED},
    # implement routes to code_review when review is enabled, otherwise
    # straight to deliverable.
    # implement routes to code_review when review is enabled, otherwise
    # straight to documenting (then deliverable).
    State.READY: {State.CODE_REVIEW, State.DOCUMENTING, State.DELIVERABLE, State.REBASING, State.ERRORED, State.BLOCKED},
    # review APPROVE -> documenting; REQUEST_CHANGES -> back to ready
    # (the implement<->review loop never touches documenting).
    State.CODE_REVIEW: {State.DOCUMENTING, State.READY, State.ERRORED, State.BLOCKED},
    # documenting always routes to deliverable — review already happened.
    State.DOCUMENTING: {State.DELIVERABLE, State.ERRORED, State.BLOCKED},
    State.DELIVERABLE: {State.HUMAN_MR_APPROVAL, State.ERRORED, State.BLOCKED},
    # PR open: merge stage polls -> merged=done, closed-unmerged=blocked,
    # conflicting=rebasing (auto-rebase cycle), failing CI=fixing_ci.
    State.HUMAN_MR_APPROVAL: {
        State.DONE,
        State.WAITING_AUTO_MERGE,
        State.REBASING,
        State.FIXING_CI,
        State.ERRORED,
        State.BLOCKED,
    },
    # rebasing: merge stage runs rebase agent -> back to human_mr_approval on
    # success, retry on failure, block on exhaustion.
    # waiting_auto_merge: merge stage polls CI; when green → done (auto-merge),
    # on CI failure → fixing_ci, on eligibility change → human_mr_approval.
    State.WAITING_AUTO_MERGE: {
        State.DONE,
        State.FIXING_CI,
        State.REBASING,
        State.HUMAN_MR_APPROVAL,
        State.ERRORED,
        State.BLOCKED,
    },
    State.REBASING: {State.HUMAN_MR_APPROVAL, State.READY, State.ERRORED, State.BLOCKED},
    # ci fix: on success -> human_mr_approval; on failure -> blocked; on crash -> errored.
    State.FIXING_CI: {State.HUMAN_MR_APPROVAL, State.BLOCKED, State.ERRORED},
    # done = merged: retrospect analyses it -> reviewed
    State.DONE: {State.CLOSED, State.ERRORED, State.BLOCKED},
    State.CLOSED: set(),
    # inquiry states: asked -> answered (terminal), or errored/blocked
    State.ASKED: {State.ANSWERED, State.ERRORED, State.BLOCKED},
    State.ANSWERED: set(),
    # epic states: open → closed or blocked; closed is terminal
    State.EPIC_OPEN: {State.EPIC_CLOSED, State.BLOCKED},
    State.EPIC_CLOSED: set(),
    # a human moves these back into the pipeline manually
    State.ERRORED: {State.READY, State.DRAFT},
    # BLOCKED: human can override to READY or DRAFT (re-run downstream),
    # or resume to the originating state (re-run only the failed stage).
    # ASKED is included so a blocked inquiry can resume to ASKED.
    State.BLOCKED: {State.READY, State.DRAFT},
}

#: active state -> name of the stage that consumes it.
STAGE_FOR_STATE: dict[State, str] = {
    State.DRAFT: "refine",
    State.READY: "implement",
    State.DOCUMENTING: "document",
    State.CODE_REVIEW: "review",
    State.DELIVERABLE: "deliver",
    State.HUMAN_MR_APPROVAL: "merge",
    State.WAITING_AUTO_MERGE: "merge",
    State.REBASING: "merge",
    State.FIXING_CI: "ci_fix",
    State.DONE: "retrospect",
    State.ASKED: "answer",
}


def can_transition(src: State, dst: State, blocked_from: State | None = None) -> bool:
    """Return True if ``src → dst`` is a legal transition.

    When *src* is ``BLOCKED``, *dst* is also allowed when it matches
    the recorded ``blocked_from`` state (the resume path).  The
    existing ``BLOCKED → READY`` and ``BLOCKED → DRAFT`` overrides are
    always available regardless of ``blocked_from``.
    Additionally, ``BLOCKED → ASKED`` is allowed when resuming a
    blocked inquiry.
    """
    allowed = TRANSITIONS.get(src, set())
    if dst in allowed:
        return True
    if src is State.BLOCKED and blocked_from is not None and dst is blocked_from:
        return True
    return False
