"""The pipeline state machine.

Each *active* state is owned by exactly one stage, which consumes
tickets in that state and transitions them onward. ``ERRORED`` and
``BLOCKED`` are side states reachable from any active state when a stage
errors or escalates; they require human attention before re-entering the
pipeline. ``HUMAN_ISSUE_APPROVAL`` is a human-wait state between refine
and implement — it has no stage owner and pauses the chain until a human
approves. ``IMPLEMENT_COMPLETE`` is an active state between
``DELIVERABLE`` and ``HUMAN_MR_APPROVAL``: the PR exists but CI and
mergeability gates have not been verified yet. The merge stage polls it
and promotes the ticket to ``HUMAN_MR_APPROVAL`` (notifying the human)
only when both CI is green and the PR is mergeable. If either gate
degrades while the ticket is in ``HUMAN_MR_APPROVAL``, the merge stage
silently falls back to ``IMPLEMENT_COMPLETE`` so the robot can auto-fix
before re-notifying. ``WAITING_AUTO_MERGE`` is an active state between
``HUMAN_MR_APPROVAL`` and ``DONE``: the merge stage detects a mergeable PR
whose CI is pending and auto-merge is eligible, then on each poll
re-checks CI; when CI goes green it auto-merges to ``DONE``, on CI
failure or conflict it falls back to ``IMPLEMENT_COMPLETE``, and on
eligibility changes it
returns to ``HUMAN_MR_APPROVAL``. ``REBASING`` is an active state between
``IMPLEMENT_COMPLETE`` and ``IMPLEMENT_COMPLETE``: the merge stage detects a
conflicting PR and transitions to ``REBASING``, then on the next poll
runs the rebase agent and force-pushes the ticket branch. On success it
returns to ``IMPLEMENT_COMPLETE`` for gate re-verification; on temporary
failure it stays in ``REBASING`` for a retry; on exhaustion it escalates
to ``BLOCKED``. ``FIXING_CI`` is an active state between
``IMPLEMENT_COMPLETE`` and ``IMPLEMENT_COMPLETE``: the merge stage detects a
mergeable PR with failing CI and transitions to ``FIXING_CI``, then on
the next poll runs the CI-fix agent. On success it returns to
``IMPLEMENT_COMPLETE`` for gate re-verification; on failure it escalates
to ``BLOCKED``. ``ADDRESSING_REVIEW`` is an active state between
``HUMAN_MR_APPROVAL`` and ``HUMAN_MR_APPROVAL``: the merge stage detects a
PR with a human reviewer's "request changes" review and transitions to
``ADDRESSING_REVIEW``, then on the next poll runs the review-revision
agent. On success it returns to ``HUMAN_MR_APPROVAL`` for re-review;
on failure with retries remaining it stays in ``ADDRESSING_REVIEW``;
on exhaustion it escalates to ``BLOCKED``.

When a ticket is blocked, the state it was blocked *from* is recorded
(``Ticket.blocked_from``). A human can **resume** the blocked ticket
straight back to that originating state to re-run only the failed stage
(no need to replay earlier stages). The existing ``BLOCKED → READY`` and
``BLOCKED → DRAFT`` transitions remain available as explicit human
overrides that re-run the full downstream chain.
"""

from __future__ import annotations

from enum import StrEnum

#: Marker prefix that agents use for [ASK_USER] comments so the
#: system can detect clarifying-question threads vs. normal comments.
ASK_USER_MARKER: str = "[ASK_USER]"


class State(StrEnum):
    DRAFT = "draft"  # raw idea, awaiting refinement
    HUMAN_ISSUE_APPROVAL = "human_issue_approval"  # refined; awaiting human approval
    READY = "ready"  # actionable; awaiting implementation
    DOCUMENTING = "documenting"  # implemented; documentation agent updating docs
    CODE_REVIEW = "code_review"  # documented; awaiting automated code review
    DELIVERABLE = "deliverable"  # reviewed; awaiting MR delivery
    HUMAN_MR_APPROVAL = "human_mr_approval"  # PR/MR open; awaiting human merge
    IMPLEMENT_COMPLETE = (
        "implement_complete"  # PR open; CI/mergeability gates not yet verified
    )
    WAITING_AUTO_MERGE = (
        "waiting_auto_merge"  # PR open + CI pending; auto-merge when green
    )
    REBASING = "rebasing"  # conflicting PR; rebase agent in progress
    FIXING_CI = "fixing_ci"  # PR open + failing CI; auto-fix in progress
    ADDRESSING_REVIEW = (
        "addressing_review"  # PR has human change requests; agent responding
    )
    DONE = "done"  # PR/MR merged; awaiting retrospect
    CLOSED = "closed"  # retrospected; pipeline complete (terminal)
    ERRORED = "errored"  # a stage threw an unhandled exception
    BLOCKED = "blocked"  # escalated; needs a human
    ASKED = "asked"  # inquiry submitted; awaiting answer
    ANSWERED = "answered"  # inquiry answered (terminal)
    AWAITING_USER_REPLY = (
        "awaiting_user_reply"  # paused mid-stage; awaiting human reply
    )
    EPIC_OPEN = "epic_open"  # epic actively collecting/grouping children
    EPIC_CLOSED = "epic_closed"  # epic closed (terminal)


#: Terminal/resolved states shared across dedup and poll sites.
DONE_OR_CLOSED: frozenset[State] = frozenset({State.CLOSED, State.DONE})

#: state -> the set of states it may transition to (the "happy path"
#: plus the always-available escalation edges).
TRANSITIONS: dict[State, set[State]] = {
    # DRAFT → EPIC_OPEN is the refine-stage promote_to_epic outcome:
    # refine decided the work is too varied to spec in one pass, flips
    # ``kind=epic`` via ``service.promote_to_epic`` and emits this
    # transition so the worker writes the canonical state event.
    State.DRAFT: {
        State.READY,
        State.HUMAN_ISSUE_APPROVAL,
        State.ERRORED,
        State.BLOCKED,
        State.CLOSED,
        State.DONE,
        State.AWAITING_USER_REPLY,
        State.EPIC_OPEN,
    },
    # human_issue_approval is a human-wait state; the human approves → ready,
    # rejects back to draft with comments, or escalates → blocked/failed.
    State.HUMAN_ISSUE_APPROVAL: {
        State.READY,
        State.DRAFT,
        State.ERRORED,
        State.BLOCKED,
    },
    # implement routes to code_review when review is enabled, otherwise
    # straight to deliverable.
    # implement routes to code_review when review is enabled, otherwise
    # straight to documenting (then deliverable).
    State.READY: {
        State.CODE_REVIEW,
        State.DOCUMENTING,
        State.DELIVERABLE,
        State.REBASING,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
        # implement-stage ``no_change_needed`` bypass: when the agent
        # confirms the spec is already satisfied by the codebase, route
        # straight to DONE. Mirrors the DELIVERABLE→DONE edge for the
        # downstream-empty-branch case.
        State.DONE,
    },
    # review APPROVE -> documenting; REQUEST_CHANGES -> back to ready
    # (the implement<->review loop never touches documenting).
    State.CODE_REVIEW: {
        State.DOCUMENTING,
        State.READY,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
    },
    # documenting always routes to deliverable — review already happened.
    State.DOCUMENTING: {
        State.DELIVERABLE,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
    },
    State.DELIVERABLE: {
        State.IMPLEMENT_COMPLETE,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
        # deliver routes to DONE directly when the branch has no new
        # commits vs origin/main — the spec was already satisfied and
        # there's nothing to ship. Mirrors refine's and implement's
        # ``no_change_needed`` bypasses.
        State.DONE,
    },
    # implement_complete: merge stage polls gates (CI + mergeability).
    # Both gates green → human_mr_approval (notify human).  CI failing →
    # fixing_ci; conflicting → rebasing; CI pending → same-state re-poll.
    State.IMPLEMENT_COMPLETE: {
        State.HUMAN_MR_APPROVAL,
        State.FIXING_CI,
        State.REBASING,
        State.WAITING_AUTO_MERGE,
        State.ADDRESSING_REVIEW,
        State.DONE,
        State.BLOCKED,
        State.ERRORED,
        State.AWAITING_USER_REPLY,
    },
    # PR open in human review: merge stage polls → merged=done,
    # closed-unmerged=blocked, gates degrade → implement_complete
    # (silent fallback — no human re-notification).
    State.HUMAN_MR_APPROVAL: {
        State.DONE,
        State.WAITING_AUTO_MERGE,
        State.IMPLEMENT_COMPLETE,
        State.ADDRESSING_REVIEW,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
    },
    # waiting_auto_merge: merge stage polls CI; when green → done (auto-merge),
    # on CI failure or conflict → implement_complete (gate re-check),
    # on eligibility change → human_mr_approval.
    State.WAITING_AUTO_MERGE: {
        State.DONE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.ADDRESSING_REVIEW,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
    },
    # rebasing: merge stage runs rebase agent → back to implement_complete on
    # success (re-verify gates), retry on failure, block on exhaustion.
    State.REBASING: {
        State.IMPLEMENT_COMPLETE,
        State.READY,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
    },
    # ci fix: on success → implement_complete (re-verify gates); on failure → blocked; on crash → errored.
    State.FIXING_CI: {
        State.IMPLEMENT_COMPLETE,
        State.BLOCKED,
        State.ERRORED,
        State.AWAITING_USER_REPLY,
    },
    # addressing review: on success → human_mr_approval (re-verify gates); on failure → blocked; on crash → errored.
    State.ADDRESSING_REVIEW: {
        State.HUMAN_MR_APPROVAL,
        State.BLOCKED,
        State.ERRORED,
        State.AWAITING_USER_REPLY,
    },
    # done = merged: retrospect analyses it -> reviewed
    State.DONE: {State.CLOSED, State.ERRORED, State.BLOCKED, State.AWAITING_USER_REPLY},
    State.CLOSED: set(),
    # inquiry states: asked -> answered (terminal), or errored/blocked
    State.ASKED: {
        State.ANSWERED,
        State.ERRORED,
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
    },
    State.ANSWERED: set(),
    # paused mid-stage: operator reply resumes to originating state (paused_from);
    # errored / blocked are the always-available escapes.
    State.AWAITING_USER_REPLY: {State.ERRORED, State.BLOCKED},
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
    State.IMPLEMENT_COMPLETE: "merge",
    State.DOCUMENTING: "document",
    State.CODE_REVIEW: "review",
    State.DELIVERABLE: "deliver",
    State.HUMAN_MR_APPROVAL: "merge",
    State.WAITING_AUTO_MERGE: "merge",
    State.REBASING: "merge",
    State.FIXING_CI: "ci_fix",
    State.ADDRESSING_REVIEW: "merge",
    State.DONE: "retrospect",
    State.ASKED: "answer",
}


def can_transition(
    src: State,
    dst: State,
    blocked_from: State | None = None,
    paused_from: State | None = None,
) -> bool:
    """Return True if ``src → dst`` is a legal transition.

    When *src* is ``BLOCKED``, *dst* is also allowed when it matches
    the recorded ``blocked_from`` state (the resume path).  The
    existing ``BLOCKED → READY`` and ``BLOCKED → DRAFT`` overrides are
    always available regardless of ``blocked_from``.
    Additionally, ``BLOCKED → ASKED`` is allowed when resuming a
    blocked inquiry.

    When *src* is ``AWAITING_USER_REPLY``, *dst* is also allowed when
    it matches the recorded ``paused_from`` state (the resume path).
    """
    allowed = TRANSITIONS.get(src, set())
    if dst in allowed:
        return True
    if src is State.BLOCKED and blocked_from is not None and dst is blocked_from:
        return True
    if (
        src is State.AWAITING_USER_REPLY
        and paused_from is not None
        and dst is paused_from
    ):
        return True
    return False
