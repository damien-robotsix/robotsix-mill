"""A retryable forge merge rejection must re-poll, not strand the ticket.

Regression guard for the class that BLOCKED seven robotsix-llmio tickets
on 2026-08-01/02: mill fired the squash merge 24-68 seconds before the
repo's required checks (``CodeQL``, ``ci / tests``) reported, GitHub
answered 405 "Required status check is expected", and mill mapped every
405 to a permanent BLOCKED. Each PR was green and mergeable a minute
later, and all seven merged untouched 33 hours on.
"""

from robotsix_mill.core.states import State
from robotsix_mill.stages.merge._shared import (
    _MERGE_MAX_RETRIES,
    _MERGE_RETRY_COUNTER,
    _merge_rejection_outcome,
    _read_counter,
)


def test_retryable_rejection_stays_in_the_merge_poll(tmp_path):
    """A retryable rejection returns same_state so the next pass retries."""
    result = {
        "merged": False,
        "retryable": True,
        "reason": 'merge not allowed: Required status check "CodeQL" is expected.',
    }

    outcome = _merge_rejection_outcome(
        "t1", tmp_path, result, same_state=State.WAITING_AUTO_MERGE
    )

    assert outcome.next_state is State.WAITING_AUTO_MERGE
    assert "CodeQL" in (outcome.note or "")
    assert _read_counter(tmp_path / _MERGE_RETRY_COUNTER) == 1


def test_non_retryable_rejection_blocks_with_the_forge_message(tmp_path):
    """A permanent refusal still fails closed — with the real reason."""
    result = {
        "merged": False,
        "reason": "merge not allowed: Merge commits are not allowed on this repository.",
    }

    outcome = _merge_rejection_outcome(
        "t1", tmp_path, result, same_state=State.WAITING_AUTO_MERGE
    )

    assert outcome.next_state is State.BLOCKED
    assert "Merge commits are not allowed" in (outcome.note or "")
    # No retry budget consumed: there is nothing to wait for.
    assert _read_counter(tmp_path / _MERGE_RETRY_COUNTER) == 0


def test_retries_are_bounded(tmp_path):
    """Past the cap the ticket blocks, so a real refusal cannot livelock."""
    result = {"merged": False, "retryable": True, "reason": "still expected"}

    states = [
        _merge_rejection_outcome(
            "t1", tmp_path, result, same_state=State.IMPLEMENT_COMPLETE
        ).next_state
        for _ in range(_MERGE_MAX_RETRIES)
    ]

    assert states[:-1] == [State.IMPLEMENT_COMPLETE] * (_MERGE_MAX_RETRIES - 1)
    assert states[-1] is State.BLOCKED
