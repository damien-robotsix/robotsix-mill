"""resume-blocked must clear a tripped implement-review cycle cap.

``ticket.implement_cycles`` is a ticket-lifetime counter checked in
implement's preflight (``phase_coordinator_preflight``) BEFORE anything
runs — before the clone, before the trace, before the agent. A ticket
sitting at ``max_implement_review_cycles`` therefore re-blocks on the very
next poll having done no work at all: no implement pass, no review, not
even a ``review.md`` in its artifacts directory.

Nothing reset it on an operator resume, which made the cap terminal while
its own block note ("escalating to BLOCKED for human inspection") invited a
human to intervene. Live on 2026-08-21 auto-mail 590f and central-deploy
de52 each absorbed two operator resumes that changed nothing, their
artifacts directories untouched between the resume and the re-block.

The rule mirrors the ``implement_spawn_count`` and ci_fix-guard precedents:
a counter at its ceiling is reset (and recorded in the event note); a
counter below its ceiling is preserved, because the resume was for some
other reason and the loop state should survive it.
"""

from robotsix_mill.core.states import State


def _block_from_ready(service, title):
    t = service.create(title)
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="Implement-review cycle limit reached")
    return t


def test_cycle_counter_at_ceiling_is_reset(service):
    """The regression: without this, resume is a silent no-op."""
    limit = service.settings.max_implement_review_cycles
    t = _block_from_ready(service, "cycles at ceiling")
    service.set_implement_cycles(t.id, limit)

    resumed = service.resume_blocked(t.id, note="root cause fixed and deployed")

    assert resumed.state is State.READY
    assert resumed.implement_cycles == 0, (
        "a ticket at the ceiling re-blocks in preflight before doing any work"
    )
    note = service.history(t.id)[-1].note
    assert "implement-review cycle counter reset" in note


def test_cycle_counter_above_ceiling_is_reset(service):
    """Defensive: the guard is `>=`, so an over-count must clear too."""
    limit = service.settings.max_implement_review_cycles
    t = _block_from_ready(service, "cycles above ceiling")
    service.set_implement_cycles(t.id, limit + 3)

    assert service.resume_blocked(t.id, note="x").implement_cycles == 0


def test_cycle_counter_below_ceiling_is_preserved(service):
    """A resume for some other reason must not hand out a free budget."""
    limit = service.settings.max_implement_review_cycles
    below = max(0, limit - 1)
    t = _block_from_ready(service, "cycles below ceiling")
    service.set_implement_cycles(t.id, below)

    resumed = service.resume_blocked(t.id, note="unrelated infra repair")

    assert resumed.implement_cycles == below
    assert "implement-review cycle counter reset" not in service.history(t.id)[-1].note


def test_reset_does_not_need_a_note(service):
    """The cap is not a judgement call — it is a counter that blocks work."""
    limit = service.settings.max_implement_review_cycles
    t = _block_from_ready(service, "cycles at ceiling, no note")
    service.set_implement_cycles(t.id, limit)

    assert service.resume_blocked(t.id).implement_cycles == 0
