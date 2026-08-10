"""Dedup guard tests for the refine stage.

These tests exercise the dedup pipeline: candidate collection, overlap
short-circuit, LLM dedup call, duplicate / already-implemented closure,
circular / invalid target refusal, and graceful degradation.

Originally part of ``test_refine.py``; moved here to keep file size
manageable.
"""

import pytest

from robotsix_mill.agents import dedup, refining
from robotsix_mill.agents.refining import TriageResult
from robotsix_mill.config import Settings
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage

from tests.agents.test_refine import _install_refine_spy, _single, ctx


@pytest.fixture(autouse=True)
def _triage_refine_ok(monkeypatch):
    """All pre-existing tests expect triage to pass through to refine.
    Tests that need a different triage outcome override this fixture."""
    monkeypatch.setattr(
        refining,
        "triage_refine",
        lambda *a, **kw: TriageResult(decision="REFINE", reason="test"),
    )


@pytest.fixture(autouse=True)
def _dedup_clean(monkeypatch):
    """Default no-op dedup so tests that don't override it still get a
    safe fallback.  Individual dedup tests override this via their own
    ``monkeypatch.setattr(dedup, "run_dedup_check", …)``."""
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        lambda **_: {"duplicate_of": None, "already_done": None, "reason": "no match"},
    )


# Substantive body — dedup is skipped for drafts under 100 chars, so
# every dedup-exercising test below needs a body comfortably above that
# threshold. Keep this in one place so the threshold can move without
# rewriting every test.
_DEDUP_BODY = (
    "This is a substantive draft body that exceeds the trivial-draft "
    "threshold of 100 characters so the dedup pipeline actually runs. "
    "Without enough body content, refine skips the dedup LLM call entirely."
)


def test_dedup_duplicate_ticket_closes(ctx, service, monkeypatch):
    """Exact-duplicate draft → CLOSED. Refine agent is never called."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t_a = service.create("Add dark mode toggle", _DEDUP_BODY)
    # Drive t_a to a refined state so it is a valid dedup target — an
    # un-refined DRAFT candidate is now rejected by _is_valid_dedup_target.
    service.transition(t_a.id, State.READY, note="refined")
    t_b = service.create("Add dark mode toggle", _DEDUP_BODY)

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": t_a.id,
            "already_done": None,
            "reason": "same change",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return orig_refine(
            settings=settings, title=title, draft=draft, repo_dir=repo_dir
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t_b, ctx)

    # Discarded drafts go to DONE so retrospect still analyses them.
    assert out.next_state is State.DONE
    assert f"duplicate of {t_a.id}" in out.note
    assert "same change" in out.note
    assert not refine_called


def test_dedup_already_committed_closes(ctx, service, monkeypatch):
    """Already-committed draft → CLOSED. Refine agent not called."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("Add X", _DEDUP_BODY)
    # A token-overlapping candidate so the zero-overlap short-circuit
    # does not skip the dedup LLM call (this test exercises the
    # already_done closure path, which must reach run_dedup_check).
    service.create("Add X again", _DEDUP_BODY)

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": "abc1234",
            "reason": "change in commit",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return orig_refine(
            settings=settings, title=title, draft=draft, repo_dir=repo_dir
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t, ctx)

    # Discarded drafts go to DONE so retrospect still analyses them.
    assert out.next_state is State.DONE
    assert "already implemented in abc1234" in out.note
    assert "change in commit" in out.note
    assert not refine_called


def test_dedup_novel_draft_proceeds_normally(ctx, service, monkeypatch):
    """Novel draft → refine runs normally, transitions to READY."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("Add X", "make x happen")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": None,
            "reason": "no match",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return orig_refine(
            settings=settings, title=title, draft=draft, repo_dir=repo_dir
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_dedup_skip_on_no_overlap_avoids_llm_call(ctx, service, monkeypatch, caplog):
    """Unrelated candidates + dedup_skip_on_no_overlap (default) →
    run_dedup_check is NOT called and refine proceeds."""
    import logging

    refine_state = _install_refine_spy(monkeypatch)

    called = {"dedup": False}

    def fake_dedup(**_):
        called["dedup"] = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    # Candidate whose tokens are disjoint from the draft.
    service.create(
        "Refactor billing invoice exporter",
        "Rework the billing invoice exporter to emit csv reports for "
        "finance reconciliation dashboards every month.",
    )
    t = service.create(
        "Zephyr quasar nebula configuration",
        "Implement zephyr quasar nebula orchestration across distributed "
        "quantum lattices ensuring photon entanglement stays coherent "
        "throughout galactic transmission windows daily.",
    )

    with caplog.at_level(logging.DEBUG, logger="robotsix_mill.stages.refine"):
        out = RefineStage().run(t, ctx)

    assert called["dedup"] is False  # LLM dedup skipped
    assert refine_state["called"] is True
    assert out.next_state is State.READY
    assert "no candidate token overlap" in caplog.text


def test_dedup_overlap_invokes_llm(ctx, service, monkeypatch):
    """A candidate sharing a meaningful token with the draft → the
    dedup LLM call IS made."""
    refine_state = _install_refine_spy(monkeypatch)

    called = {"dedup": False}

    def fake_dedup(**_):
        called["dedup"] = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    service.create("Add dark mode toggle", _DEDUP_BODY)
    t = service.create("Add dark mode toggle", _DEDUP_BODY)

    out = RefineStage().run(t, ctx)

    assert called["dedup"] is True
    assert refine_state["called"] is True
    assert out.next_state is State.READY


def test_build_candidates_block_truncates_long_body(ctx, service):
    """A candidate body longer than the cap is truncated with a marker;
    the rendered block stays bounded."""
    from robotsix_mill.stages.refine import _build_candidates_block

    long_body = "word " * 2000  # ~10k chars
    t = service.create("Some candidate", long_body)
    block = _build_candidates_block([service.get(t.id)], ctx)

    assert "description truncated" in block
    assert len(block) < ctx.settings.dedup_candidate_body_max_chars + 500


def test_build_candidates_block_keeps_short_body(ctx, service):
    """A short candidate body is rendered unchanged (no truncation)."""
    from robotsix_mill.stages.refine import _build_candidates_block

    short = "A concise candidate body well under the cap."
    t = service.create("Short candidate", short)
    block = _build_candidates_block([service.get(t.id)], ctx)

    assert short in block
    assert "description truncated" not in block


def test_build_candidates_block_no_truncation_when_cap_disabled(
    service, repo_config, tmp_path
):
    """A cap of 0 disables truncation entirely."""
    from robotsix_mill.stages.refine import _build_candidates_block

    long_body = "word " * 2000
    t = service.create("Some candidate", long_body)
    settings0 = Settings(data_dir=str(tmp_path), dedup_candidate_body_max_chars=0)
    ctx0 = StageContext(settings=settings0, service=service, repo_config=repo_config)
    block = _build_candidates_block([service.get(t.id)], ctx0)

    assert "description truncated" not in block
    assert long_body.strip() in block


def test_dedup_circular_target_refused(ctx, service, monkeypatch):
    """Reproduce the 3191/d0fc circular case: A was closed as a
    duplicate of B; a later dedup run on B that proposes
    ``already_done = A`` must be refused so the blocker stays tracked."""
    refine_state = _install_refine_spy(monkeypatch)

    t_b = service.create("Consume llmio CostLogSource read-port", _DEDUP_BODY)
    t_a = service.create("Blocked: merged llmio CostLogSource read", _DEDUP_BODY)

    # A was closed as a duplicate of B (DRAFT→DONE→CLOSED).
    service.transition(t_a.id, State.DONE, note=f"duplicate of {t_b.id}: same blocker")
    service.transition(t_a.id, State.CLOSED, note="closed")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": t_a.id,
            "reason": "already covered",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t_b, ctx)

    # The circular close must NOT happen — refine proceeds instead.
    assert out.next_state is State.READY
    assert refine_state["called"]
    assert "already_done" not in (out.note or "")
    assert "already implemented in" not in (out.note or "")


def test_dedup_closed_as_duplicate_of_third_ticket_refused(ctx, service, monkeypatch):
    """A candidate closed as a duplicate of some *other* ticket (not
    circular) is still a non-implementation closure → refine proceeds."""
    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add widget", _DEDUP_BODY)
    t_x = service.create("Unrelated tracker", _DEDUP_BODY)
    cand = service.create("Add widget (older)", _DEDUP_BODY)

    # cand was dedup-closed against a third ticket X (DONE→CLOSED).
    service.transition(cand.id, State.DONE, note=f"duplicate of {t_x.id}: same")
    service.transition(cand.id, State.CLOSED, note="closed")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": cand.id,
            "already_done": None,
            "reason": "looks similar",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]
    assert "duplicate of" not in (out.note or "")


def test_dedup_declined_candidate_refused(ctx, service, monkeypatch):
    """A declined candidate (CLOSED, never DONE) is not a fix → refine
    proceeds rather than closing the ticket against it."""
    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add gadget", _DEDUP_BODY)
    cand = service.create("Add gadget (declined)", _DEDUP_BODY)

    # Declined as noise: DRAFT → CLOSED directly, never DONE.
    service.transition(cand.id, State.CLOSED, note="declined as noise")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": cand.id,
            "already_done": None,
            "reason": "looks similar",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]


def test_dedup_legit_implemented_candidate_accepted(ctx, service, monkeypatch):
    """A candidate that reached DONE via a real implementation/merge
    note (no non-implementation prefix) remains a valid dedup target."""
    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add feature Z", _DEDUP_BODY)
    cand = service.create("Add feature Z (shipped)", _DEDUP_BODY)

    # Genuinely implemented and merged — set a branch so the
    # human-closed-with-claim guard (gates.py) does not reject this
    # candidate as an unverified external-fix claim.
    service.set_branch(cand.id, "feat/z")
    service.transition(cand.id, State.DONE, note="implemented and merged in PR #7")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": cand.id,
            "reason": "already shipped",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert f"already implemented in {cand.id}" in out.note
    assert not refine_state["called"]


def test_dedup_unmerged_candidate_branch_refused(ctx, service, monkeypatch):
    """A candidate that reached DONE via a real implementation note but
    whose own branch never merged to main must NOT close the current
    ticket as a duplicate — refine proceeds so the stranded work is
    re-applied."""
    from robotsix_mill.stages import refine as refine_module

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Re-apply stranded work", _DEDUP_BODY)
    cand = service.create("Original (stranded)", _DEDUP_BODY)

    # Genuinely implemented (passes all four pre-merge validity checks)
    # and carries a branch — but that branch never merged.
    service.set_branch(cand.id, "feat/stranded")
    service.transition(cand.id, State.DONE, note="implemented in PR #7")

    # Report the candidate's branch as unmerged, decoupling the test
    # from a real git repo.
    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, t: False
    )

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": cand.id,
            "reason": "already shipped",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]
    assert "already implemented in" not in (out.note or "")


def test_dedup_merged_candidate_branch_accepted(ctx, service, monkeypatch):
    """Positive control: a DONE candidate whose branch IS merged stays a
    valid dedup target — the current ticket is still closed DONE."""
    from robotsix_mill.stages import refine as refine_module

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add feature Z", _DEDUP_BODY)
    cand = service.create("Add feature Z (shipped)", _DEDUP_BODY)

    service.set_branch(cand.id, "feat/z")
    service.transition(cand.id, State.DONE, note="implemented in PR #7")

    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, t: True
    )

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": cand.id,
            "reason": "already shipped",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert f"already implemented in {cand.id}" in out.note
    assert not refine_state["called"]


def test_dedup_skipped_for_empty_title_and_draft(ctx, service, monkeypatch):
    """When both title and draft are empty, blocks BEFORE dedup check."""
    dedup_called = False

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(service.create("", "   "), ctx)
    assert out.next_state is State.BLOCKED
    assert "empty title and draft" in out.note
    assert not dedup_called


def test_dedup_skipped_for_trivial_draft(ctx, service, monkeypatch):
    """Trivial drafts (body <100 chars) skip dedup — the LLM call cost
    dwarfs the value when there's barely anything to compare. Refine
    still proceeds normally."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    dedup_called = False

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    # body="" → trivial → dedup must be skipped.
    out = RefineStage().run(service.create("Add dark mode toggle", ""), ctx)
    assert out.next_state is State.READY
    assert not dedup_called, "dedup should be skipped for trivial drafts"


def test_dedup_never_flags_self(ctx, service, monkeypatch):
    """The candidates block passed to dedup must NOT mention the
    current ticket's id."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("my ticket", _DEDUP_BODY)
    # Create another ticket so the candidate list isn't empty
    service.create("other ticket", "other draft")

    seen_block = None

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal seen_block
        seen_block = candidates_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    RefineStage().run(t, ctx)

    assert seen_block is not None
    # The candidates block is one ``## <id>`` section per ticket; the
    # current ticket's id must not appear as a section heading.
    assert f"## {t.id}" not in seen_block


def test_dedup_candidate_bodies_included(ctx, service, monkeypatch):
    """Candidate entries passed to dedup must include each ticket's
    full description body inside a ``<body>...</body>`` block."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    # Create the current ticket (will be excluded from candidates).
    t = service.create("my ticket", _DEDUP_BODY)

    # Create two candidate tickets with distinctive bodies.
    t_a = service.create("candidate A", "body of ticket A\nline two")
    t_b = service.create("candidate B", "body of ticket B")

    seen_block = None

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal seen_block
        seen_block = candidates_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    RefineStage().run(t, ctx)

    assert seen_block is not None
    # Each candidate is a Markdown section with title + body.
    assert f"## {t_a.id}" in seen_block
    assert "- title: candidate A" in seen_block
    assert "body of ticket A\nline two" in seen_block

    assert f"## {t_b.id}" in seen_block
    assert "- title: candidate B" in seen_block
    assert "body of ticket B" in seen_block

    # Each section uses the <body>...</body> framing.
    assert seen_block.count("````body") == 2
    assert seen_block.count("````\n<!-- /body -->") == 2


def test_dedup_failure_degrades_gracefully(ctx, service, monkeypatch):
    """Dedup check raises → refine proceeds normally."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("Add X", "make x happen")

    def boom_dedup(*, settings, draft_title, draft_body, candidates_json):
        raise RuntimeError("dedup model down")

    monkeypatch.setattr(dedup, "run_dedup_check", boom_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return orig_refine(
            settings=settings, title=title, draft=draft, repo_dir=repo_dir
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_dedup_clone_failure_escalates_before_dedup(ctx, service, monkeypatch):
    """Clone failure propagates to the worker before dedup runs at all —
    no half-grounded refine attempts. The stage no longer catches
    CalledProcessError — the worker owns the retry/block decision."""
    import subprocess

    from robotsix_mill.vcs import git_ops

    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    ctx.settings.forge_remote_url = "https://example.test/repo.git"
    dedup_called = False

    def boom_clone(url, dest, branch, token, **kwargs):
        raise subprocess.CalledProcessError(128, "git", stderr="no access")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(git_ops, "clone", boom_clone)
    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    t = service.create("Add X", _DEDUP_BODY)

    with pytest.raises(subprocess.CalledProcessError):
        RefineStage().run(t, ctx)

    assert not dedup_called, "dedup should not be called when clone failed"


def test_draft_to_closed_transition_is_legal():
    """DRAFT → CLOSED is a valid transition in the state machine."""
    from robotsix_mill.core.states import State as S
    from robotsix_mill.core.states import can_transition

    assert can_transition(S.DRAFT, S.CLOSED) is True


def test_dedup_guard_survives_preexisting_closed_ticket(ctx, service, monkeypatch):
    """Regression: SQLite used to return updated_at tz-naive; the dedup
    guard compared it to a tz-aware cutoff and raised TypeError, ERRORing
    every draft once any CLOSED ticket existed. After the model fix,
    updated_at is timezone-aware and comparisons are safe."""
    old = service.create("old done thing", "stuff")
    service.transition(old.id, State.CLOSED)  # now a closed candidate
    # Re-read via list() the way refine does.
    closed = next(t for t in service.list() if t.id == old.id)
    assert closed.updated_at.tzinfo is not None

    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )
    t = service.create("Add Y", "rough idea")
    out = RefineStage().run(t, ctx)  # must NOT raise TypeError
    assert out.next_state is not State.ERRORED


def test_dedup_parent_filter_narrows_candidates(ctx, service, monkeypatch):
    """When the draft ticket belongs to an epic (has parent_id),
    the candidates passed to dedup are filtered to only siblings,
    the parent epic itself, orphans, and recently-closed tickets."""
    # Epic A — the draft's parent
    epic_a = service.create(
        "Epic A: Agent Memory", "memory system", kind=TicketKind.EPIC
    )
    # Epic B — unrelated
    epic_b = service.create(
        "Epic B: Deploy Config", "deployment things", kind=TicketKind.EPIC
    )

    # Draft ticket — child of epic A
    draft_ticket = service.create(
        "Add LRU eviction",
        _DEDUP_BODY,
        parent_id=epic_a.id,
    )

    # Sibling — same epic, should appear
    sibling = service.create(
        "Add TTL-based expiry",
        _DEDUP_BODY,
        parent_id=epic_a.id,
    )

    # Open ticket in unrelated epic — should NOT appear
    unrelated_open = service.create(
        "Switch to k3s",
        _DEDUP_BODY,
        parent_id=epic_b.id,
    )

    # Orphan (no parent) — should appear
    orphan = service.create("Upgrade CI runner", _DEDUP_BODY)

    # Recently-closed cross-epic ticket — should appear
    cross_epic_closed = service.create(
        "Old deploy fix",
        _DEDUP_BODY,
        parent_id=epic_b.id,
    )
    service.transition(cross_epic_closed.id, State.CLOSED)

    # Another epic that is NOT the draft's parent — should NOT appear
    unrelated_epic = service.create(
        "Epic C: Observability", "metrics", kind=TicketKind.EPIC
    )

    # Non-sibling open ticket in same epic is the only non-CLOSED,
    # non-orphan, non-parent candidate that SHOULD appear (sibling).
    # All the others from epic B should be excluded.

    seen_candidates: list[str] = []

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        seen_candidates.append(candidates_json)
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )

    out = RefineStage().run(draft_ticket, ctx)
    assert out.next_state is State.READY
    assert len(seen_candidates) == 1

    candidates_text = seen_candidates[0]

    # Should appear: sibling, parent epic, orphan, recently-closed cross-epic
    assert f"## {sibling.id}" in candidates_text
    assert f"## {epic_a.id}" in candidates_text  # parent epic
    assert f"## {orphan.id}" in candidates_text
    assert f"## {cross_epic_closed.id}" in candidates_text

    # Should NOT appear: unrelated open, unrelated epic
    assert f"## {unrelated_open.id}" not in candidates_text
    assert f"## {unrelated_epic.id}" not in candidates_text

    # Draft itself should never be a candidate
    assert f"## {draft_ticket.id}" not in candidates_text


def test_dedup_no_parent_fallback_unchanged(ctx, service, monkeypatch):
    """When the draft ticket has no parent_id, the full candidate set
    is passed through — behaviour is identical to before."""
    t = service.create("Standalone ticket", _DEDUP_BODY)
    # Create several tickets with various parents — all should appear.
    epic = service.create("Some epic", "stuff", kind=TicketKind.EPIC)
    child = service.create("Epic child", _DEDUP_BODY, parent_id=epic.id)
    orphan = service.create("Another orphan", _DEDUP_BODY)

    seen_candidates: list[str] = []

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        seen_candidates.append(candidates_json)
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )

    out = RefineStage().run(t, ctx)
    assert out.next_state is State.READY
    assert len(seen_candidates) == 1

    candidates_text = seen_candidates[0]

    # All non-epic tickets should appear (epics are always excluded
    # unless they're the draft's own parent, which doesn't apply here).
    assert f"## {child.id}" in candidates_text
    assert f"## {orphan.id}" in candidates_text
    assert f"## {epic.id}" not in candidates_text  # epics excluded


def test_dedup_candidate_cap_enforced(ctx, service, monkeypatch):
    """Create 12 candidate tickets (above the default max of 8), run
    dedup, verify the candidate block contains at most 8 sections."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    # Create the draft ticket.
    draft_ticket = service.create("draft ticket", _DEDUP_BODY)

    # Create 12 candidate tickets with diverse titles.
    titles = [
        "Add dark mode toggle",
        "Fix login timeout bug",
        "Refactor database layer",
        "Update README badges",
        "Rate limiting middleware",
        "CSV export feature",
        "CI pipeline improvements",
        "Add healthcheck endpoint",
        "Add user avatar field",
        "Implement search functionality",
        "Upgrade to Python 3.14",
        "Add WebSocket support",
    ]
    for title in titles:
        service.create(title, "some body text for candidate ticket")

    seen_block = None

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal seen_block
        seen_block = candidates_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(draft_ticket, ctx)

    assert out.next_state is State.READY
    assert seen_block is not None

    # Count candidate sections (each is "## <id>").
    # The default dedup_max_candidates is 8, so at most 8 sections.
    section_count = seen_block.count("\n## ")
    # "(no candidates)" has zero sections.
    assert section_count <= 8, (
        f"expected at most 8 candidate sections, got {section_count}"
    )
