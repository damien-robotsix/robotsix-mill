"""Implement stage: preflight epic context, spec-fingerprint guards and
resume-with-ahead edit-claim guard.

Extracted from ``test_implement.py`` (module-size split).
"""

import contextlib
import hashlib
from pathlib import Path

from robotsix_mill.agents import coding
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import ImplementStage
from tests.stages.implement.conftest import (
    _ticket,
    _write_file_map,
    make_bare_repo,
)

# --- epic context in preflight spec check --------------------------------


def test_preflight_epic_context_allows_empty_direct_spec(
    ctx_factory, tmp_path, monkeypatch
):
    """An epic child with an empty direct body but non-empty parent epic
    must pass the preflight spec gate (epic context inherited)."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    # Parent epic with real content.
    epic = ctx.service.create(
        "Epic parent", "Build the login system", kind=TicketKind.EPIC
    )
    # Child with empty body — spec inherited from epic.
    child = ctx.service.create("Epic child", "", parent_id=epic.id)
    _write_file_map(ctx, child, "feature.txt")

    # preflight should NOT block — epic context provides the spec.
    out = ImplementStage().preflight(child, ctx)
    assert out is None, f"epic context should satisfy spec gate, got: {out}"


def test_preflight_blocks_when_both_spec_and_epic_empty(
    ctx_factory, tmp_path, monkeypatch
):
    """When BOTH the direct spec AND the epic context are empty,
    preflight must block."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    # Parent epic with empty body.
    epic = ctx.service.create("Empty epic", "", kind=TicketKind.EPIC)
    # Child with empty body — no spec from either source.
    child = ctx.service.create("Empty child", "", parent_id=epic.id)
    _write_file_map(ctx, child, "feature.txt")

    out = ImplementStage().preflight(child, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "empty or missing specification" in out.note.lower()


# --- stale re-spawn guard (spec fingerprint) ----------------------------


def test_stale_respawn_guard_blocks_unchanged_spec(ctx_factory, tmp_path, monkeypatch):
    """When implement.md records a BLOCKED outcome and the spec hasn't
    changed, preflight must block BEFORE a trace opens."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Stale spec", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # First implement run: agent produces NO changes → BLOCKED.
    # This writes implement.md with "BLOCKED — resumable" + spec-fingerprint.
    def _agent_noop(*, repo_dir, spec, **kwargs):
        return ("did nothing", [], "", None, None, True, "nothing to do")

    monkeypatch.setattr(coding, "run_implement_agent", _agent_noop)

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.DONE  # no_change_needed → DONE

    # Reset ticket to READY to simulate a re-spawn.
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    t = ctx.service.get(t.id)

    # Write implement.md simulating a prior BLOCKED outcome.
    ws = ctx.service.workspace(t)
    # The BLOCKED→READY transition above persists a spec-fingerprint
    # override (durable suppress).  Clear it here so this test
    # exercises the non-override (guard-fires) path.
    override = ws.artifacts_dir / "implement_spec_override"
    with contextlib.suppress(FileNotFoundError):
        override.unlink()

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should block — spec unchanged, last outcome BLOCKED.
    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spec unchanged" in out.note.lower()
    assert fp in out.note


def test_stale_respawn_guard_allows_changed_spec(ctx_factory, tmp_path, monkeypatch):
    """When the spec has changed since the last BLOCKED implement,
    preflight must allow the re-spawn."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Changed spec", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Write implement.md with fingerprint of the OLD spec.
    ws = ctx.service.workspace(t)

    old_body = "Old spec content"
    old_fp = hashlib.sha256(old_body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {old_fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should allow — current spec differs from stored fingerprint.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when spec changed, got: {out}"


def test_stale_respawn_guard_skips_when_passed(ctx_factory, tmp_path, monkeypatch):
    """When implement.md records a 'passed' outcome, preflight must NOT
    block regardless of fingerprint match."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Passed ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Write implement.md with "passed" header — even with matching
    # fingerprint, preflight should not block.
    ws = ctx.service.workspace(t)

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (passed)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\ncompleted successfully\n",
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when last outcome passed, got: {out}"


def test_stale_respawn_guard_skips_without_implement_md(
    ctx_factory, tmp_path, monkeypatch
):
    """On first implement run (no implement.md), preflight must proceed
    normally — no false positive block."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Fresh ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # No implement.md exists — preflight should proceed.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow on first run, got: {out}"


# --- transient fingerprint guard -------------------------------------------


def test_transient_header_skips_fingerprint_guard(ctx_factory, tmp_path, monkeypatch):
    """When implement.md records a TRANSIENT outcome, preflight must NOT
    block — the transient abort was environmental, not spec-determined."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transient ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Write implement.md with "TRANSIENT — retryable" header and NO
    # spec-fingerprint line — simulating an env-error short-circuit.
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (TRANSIENT — retryable)\n"
        "branch: test-branch\n"
        "\nenvironment failure not fixable by code edits\n",
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)
    assert out is None, (
        f"preflight must allow when last outcome was TRANSIENT, got: {out}"
    )


def test_spec_determined_blocked_persists_fingerprint_and_guards(
    ctx_factory, tmp_path, monkeypatch
):
    """A spec-determined BLOCKED writes the fingerprint, and an unchanged-
    spec re-spawn is correctly guarded."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Blocked spec-determined", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Run an implement pass that produces NO changes → _finalize writes
    # implement.md with "BLOCKED — resumable" + spec-fingerprint.
    def _agent_noop(*, repo_dir, spec, **kwargs):
        return ("did nothing", [], "", None, None, True, "nothing to do")

    monkeypatch.setattr(coding, "run_implement_agent", _agent_noop)

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.DONE  # no_change_needed → DONE

    # Reset ticket to READY to simulate a re-spawn.
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    t = ctx.service.get(t.id)

    # Write implement.md with "BLOCKED — resumable" + matching fingerprint.
    ws = ctx.service.workspace(t)
    # The BLOCKED→READY transition above persists a spec-fingerprint
    # override (durable suppress).  Clear it here so this test
    # exercises the non-override (guard-fires) path.
    override = ws.artifacts_dir / "implement_spec_override"
    with contextlib.suppress(FileNotFoundError):
        override.unlink()

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should block — spec-determined, fingerprint matches.
    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spec-determined" in out.note.lower()
    assert fp in out.note


# --- fingerprint collision + operator force-retry integration ------------


def test_resume_blocked_with_note_allows_implement_after_fingerprint_match(
    ctx_factory, tmp_path, monkeypatch
):
    """A blocked ticket with an unchanged spec, resumed with an operator
    justification note, must proceed to a fresh implement cycle, and
    the guard stays suppressed for that spec fingerprint across
    subsequent cycles — only re-blocking when the spec actually changes."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Force-retry ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # --- Phase 1: write a stale implement.md simulating a prior BLOCKED ---
    # outcome with the current spec's fingerprint.
    ws = ctx.service.workspace(t)

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should block — fingerprint matches unchanged spec.
    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spec unchanged" in out.note.lower()
    assert fp in out.note

    # Transition the ticket to BLOCKED (simulating what the worker would do).
    ctx.service.transition(t.id, State.BLOCKED, out.note)
    t = ctx.service.get(t.id)
    assert t.state is State.BLOCKED

    # --- Phase 2: operator force-retry via resume_blocked with note ---
    # This should clear the stale implement guard.
    resumed = ctx.service.resume_blocked(t.id, note="operator force retry — flake")
    assert resumed.state is State.READY
    assert not (ws.artifacts_dir / "implement.md").exists(), (
        "resume_blocked with note must clear the stale implement guard"
    )

    # --- Phase 3: preflight must now ALLOW the re-spawn ---
    # (implement.md was deleted, so the guard has nothing to match against).
    t = ctx.service.get(t.id)
    out3 = ImplementStage().preflight(t, ctx)
    assert out3 is None, f"preflight must allow after force-retry, got: {out3}"

    # --- Phase 4: after ONE implement cycle (which fails again), ---
    # the guard re-arms — the next automatic retry should be blocked.
    def _agent_noop(*, repo_dir, spec, **kwargs):
        return ("did nothing", [], "", None, None, True, "nothing to do")

    monkeypatch.setattr(coding, "run_implement_agent", _agent_noop)

    out4 = ImplementStage().run(t, ctx)
    assert out4.next_state is State.DONE  # no_change_needed → DONE

    # Write a fresh implement.md with matching fingerprint (simulating a
    # new BLOCKED outcome from a subsequent re-spawn).
    ctx.service.transition(t.id, State.BLOCKED, "test re-block")
    ctx.service.transition(t.id, State.READY, "test re-block")
    t = ctx.service.get(t.id)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should NOT block — the durable override persists across
    # cycles for the same spec fingerprint (operator already overrode).
    out5 = ImplementStage().preflight(t, ctx)
    assert out5 is None, (
        f"durable override must suppress re-block for same fingerprint, got: {out5}"
    )


def test_resume_blocked_without_note_clears_fingerprint_guard(
    ctx_factory, tmp_path, monkeypatch
):
    """resume_blocked without a note clears the fingerprint guard — the
    resume itself is the operator's authorization to re-run the stage, so
    the next preflight must NOT block on an unchanged spec."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="No-note retry", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Write implement.md with matching fingerprint and block the ticket.
    ws = ctx.service.workspace(t)

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    ctx.service.transition(t.id, State.BLOCKED, "fingerprint match")
    t = ctx.service.get(t.id)
    assert t.state is State.BLOCKED

    # Resume WITHOUT a note — the guard SHOULD be cleared.
    resumed = ctx.service.resume_blocked(t.id)
    assert resumed.state is State.READY
    assert not (ws.artifacts_dir / "implement.md").exists(), (
        "resume_blocked without note must clear the implement guard"
    )

    # Preflight should NOT block on the unchanged spec — it proceeds.
    t = ctx.service.get(t.id)
    out = ImplementStage().preflight(t, ctx)
    assert out is None


def test_transition_blocked_to_ready_with_note_clears_fingerprint_guard(
    ctx_factory, tmp_path, monkeypatch
):
    """An operator-forced BLOCKED→READY transition with a justification
    note also clears the fingerprint guard (equivalent to resume_blocked)."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transition retry", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    ctx.service.transition(t.id, State.BLOCKED, "fingerprint match")
    t = ctx.service.get(t.id)
    assert t.state is State.BLOCKED

    # Force-retry via direct transition with a note (not resume_blocked).
    ctx.service.transition(
        t.id, State.READY, note="operator force retry — environmental flake"
    )
    t = ctx.service.get(t.id)
    assert t.state is State.READY
    assert not (ws.artifacts_dir / "implement.md").exists(), (
        "BLOCKED→READY transition with note must clear the implement guard"
    )

    # Preflight must now allow the re-spawn.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow after transition with note, got: {out}"


def test_answer_pending_question_clears_fingerprint_guard(
    ctx_factory, tmp_path, monkeypatch
):
    """When an AWAITING_USER_REPLY ticket with a stale implement fingerprint
    is answered via answer_pending_question, the fingerprint guard must be
    cleared — the operator's answer is the new input, and re-blocking on
    the unchanged spec alone wastes the spawn budget."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Comment, Ticket

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Answer-clears-guard", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Put the ticket into AWAITING_USER_REPLY with an open ask_user thread.
    with db.session(ctx.settings, ctx.service.board_id) as s:
        row = s.get(Ticket, t.id)
        row.state = State.AWAITING_USER_REPLY
        row.paused_from = State.READY.value
        s.add(row)
        s.flush()
        # Add an open [ASK_USER] thread.
        c = Comment(
            ticket_id=t.id,
            body="[ASK_USER] operator clarification needed",
            author="system",
            parent_id=None,
        )
        s.add(c)
        s.commit()

    t = ctx.service.get(t.id)
    assert t.state is State.AWAITING_USER_REPLY

    # Operator answers the question.
    reply = ctx.service.answer_pending_question(
        t.id,
        "Here is the clarification.",
        author="operator",
    )
    assert reply is not None

    # Ticket must resume to READY (its paused_from).
    t = ctx.service.get(t.id)
    assert t.state is State.READY

    # The spec-fingerprint override must be persisted.
    override_path = ws.artifacts_dir / "implement_spec_override"
    assert override_path.exists(), (
        "answer_pending_question must persist the spec-fingerprint override"
    )
    assert override_path.read_text(encoding="utf-8").strip() == fp, (
        "override fingerprint must match the current spec fingerprint"
    )

    # Preflight must now ALLOW the re-spawn.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, (
        f"preflight must allow after answer_pending_question, got: {out}"
    )


def test_automatic_fingerprint_refusal_message_includes_force_retry_remedy(
    ctx_factory, tmp_path, monkeypatch
):
    """When the fingerprint guard blocks an automatic retry, the diagnostic
    message must name resume-blocked as an operator remedy."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Message check", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    # The message must mention both the old remedies AND the new one.
    assert "resume-blocked" in out.note, (
        f"diagnostic must mention resume-blocked as a remedy, got: {out.note}"
    )
    assert "fingerprint" in out.note
    assert "spec unchanged" in out.note.lower()


# ---------------------------------------------------------------------------
# resume-with-ahead edit-claim guard: a branch that already carries committed
# work is not "lost work", even when one claimed edit is missing from its diff.
#
# Measured 2026-08-13: four of the five tickets blocked on this path had 2-4
# commits and 2-6 changed files sitting in their workspace, stranded
# (robotsix-chat a78d/a801/3f3e, robotsix-mill 8b2c). The fifth (959e) had an
# empty branch — the shape the guard is actually for.
# ---------------------------------------------------------------------------


def _edit_msgs(*paths: str) -> bytes:
    """A new_messages payload claiming an ``edit_file`` call per path."""
    import json as _json

    parts: list[dict] = []
    for p in paths:
        parts.append(
            {
                "part_kind": "tool-call",
                "tool_name": "edit_file",
                "args": {"path": p},
                "tool_call_id": f"call_{p}",
            }
        )
    return _json.dumps([{"parts": parts}]).encode()


def test_resume_with_ahead_missing_claimed_edit_does_not_block(
    ctx_factory, tmp_path, monkeypatch
):
    """Branch has real commits; one claimed edit is absent → proceed, not BLOCK.

    This is the stranded-work shape: prior passes committed the
    implementation, the resume pass re-touched files and also claimed an
    edit that is not in the branch diff (a changelog fragment renamed away,
    a file written then reverted). Blocking discards committed work and
    needs a human; the branch is intact, so it must flow on to review.
    """
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="true"
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _run_first(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_first)
    assert ImplementStage().run(t, ctx).next_state is State.CODE_REVIEW

    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)

    # Resume: no new working-tree changes, but the agent claims an edit to a
    # file that never made it onto the branch.
    def _run_resume(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            "Re-applied the change; also wrote a changelog fragment.",
            [],
            "",
            None,
            _edit_msgs("never_landed.md"),
            False,
            "",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run_resume)
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.CODE_REVIEW
    assert "edit-claim contradiction" not in (out.note or "")


def test_resume_empty_branch_missing_claimed_edit_still_blocks(
    ctx_factory, tmp_path, monkeypatch
):
    """No commits on the branch + a claimed edit that vanished → still BLOCK.

    Keeps the guard's teeth where the evidence says it belongs: nothing was
    committed, so an edit call that left no trace really is lost work.
    """
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="true"
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # First pass commits nothing — the branch stays level with origin/main.
    def _run_noop(*, repo_dir, **_kwargs):
        del repo_dir
        return ("nothing yet", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_noop)
    ImplementStage().run(t, ctx)

    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)

    def _run_resume(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            "Edited the file.",
            [],
            "",
            None,
            _edit_msgs("vanished.py"),
            False,
            "",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run_resume)
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
