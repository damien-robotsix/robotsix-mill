"""resume-blocked must clear the ci_fix guard counters it is blocked on.

Two counters bound the merge-side CI loop, and both are evaluated
BEFORE the ci-fix agent is allowed to run:

* ``ci_identical_failure_count.txt`` vs ``ci_fix_max_identical_failures``
* ``auto_fix_cycles.txt`` vs ``auto_fix_max_cycles``

Nothing cleared either on an operator resume, which made both guards
terminal: the gate re-evaluates on the next poll, finds the counter
still at its ceiling, and re-blocks with the count one higher. Live on
2026-08-11 a robotsix-ui ticket carried five consecutive resume ->
re-block pairs against the identical fingerprint — its block note
advising "Resume to retry" every time, advice the code could not honour.

The rule mirrors the existing ``implement_spawn_count`` precedent: a
counter at its ceiling is reset (and recorded in the event note); a
counter below its ceiling is preserved, because the resume was for some
other reason and the loop state should survive it.
"""

from robotsix_mill.core.states import State

_IDENTICAL = "ci_identical_failure_count.txt"
_FINGERPRINT = "ci_failure_fingerprint.txt"
_AUTO_FIX = "auto_fix_cycles.txt"


def _block_from_fixing_ci(service, title):
    """Drive a ticket down the real merge path into BLOCKED from FIXING_CI."""
    t = service.create(title)
    for state in (
        State.READY,
        State.CODE_REVIEW,
        State.DOCUMENTING,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.FIXING_CI,
    ):
        service.transition(t.id, state)
    service.transition(t.id, State.BLOCKED, note="same CI failure fingerprint")
    return t


def test_identical_failure_counter_at_ceiling_is_reset(service):
    """The regression: without this, resuming re-blocks on the next poll."""
    t = _block_from_fixing_ci(service, "identical failure at ceiling")
    ws = service.workspace(t)
    counter = ws.artifacts_dir / _IDENTICAL
    fingerprint = ws.artifacts_dir / _FINGERPRINT
    limit = service.settings.ci_fix_max_identical_failures
    counter.write_text(str(limit), encoding="utf-8")
    fingerprint.write_text("fef2a01336e57206", encoding="utf-8")

    resumed = service.resume_blocked(t.id, note="fixed the flaky assertion")

    assert resumed.state is State.FIXING_CI
    assert not counter.exists()
    # The stored fingerprint is the counter's comparison baseline —
    # leaving it would walk straight back up to the ceiling against the
    # same failure without the agent ever being asked.
    assert not fingerprint.exists()


def test_auto_fix_cycle_counter_at_ceiling_is_reset(service):
    t = _block_from_fixing_ci(service, "auto fix cycles at ceiling")
    ws = service.workspace(t)
    counter = ws.artifacts_dir / _AUTO_FIX
    counter.write_text(str(service.settings.auto_fix_max_cycles), encoding="utf-8")

    service.resume_blocked(t.id)

    assert not counter.exists()


def test_counters_below_their_ceiling_are_preserved(service):
    """A resume for an unrelated reason must not wipe live loop state."""
    t = _block_from_fixing_ci(service, "counters below ceiling")
    ws = service.workspace(t)
    identical = ws.artifacts_dir / _IDENTICAL
    fingerprint = ws.artifacts_dir / _FINGERPRINT
    auto_fix = ws.artifacts_dir / _AUTO_FIX
    identical.write_text("0", encoding="utf-8")
    fingerprint.write_text("abc123", encoding="utf-8")
    auto_fix.write_text("1", encoding="utf-8")

    service.resume_blocked(t.id, note="transient runner outage")

    assert identical.read_text(encoding="utf-8").strip() == "0"
    assert fingerprint.read_text(encoding="utf-8").strip() == "abc123"
    assert auto_fix.read_text(encoding="utf-8").strip() == "1"
    assert "ci_fix guard" not in (service.history(t.id)[-1].note or "")


def test_the_reset_is_recorded_in_the_event_history(service):
    """An operator reading the board must see which guard was cleared."""
    t = _block_from_fixing_ci(service, "reset recorded in history")
    ws = service.workspace(t)
    (ws.artifacts_dir / _IDENTICAL).write_text(
        str(service.settings.ci_fix_max_identical_failures), encoding="utf-8"
    )
    (ws.artifacts_dir / _AUTO_FIX).write_text(
        str(service.settings.auto_fix_max_cycles), encoding="utf-8"
    )

    service.resume_blocked(t.id, note="rebased onto green main")

    note = service.history(t.id)[-1].note or ""
    assert "resumed from blocked" in note
    assert "override: rebased onto green main" in note
    assert "ci_fix guard(s) reset via resume-blocked" in note
    assert _IDENTICAL in note
    assert _AUTO_FIX in note


def test_a_resume_with_no_ci_artifacts_is_a_no_op(service):
    """Most tickets never reach the CI loop; they must resume unchanged."""
    t = service.create("no ci artifacts")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")

    resumed = service.resume_blocked(t.id)

    assert resumed.state is State.READY
    assert "ci_fix guard" not in (service.history(t.id)[-1].note or "")
