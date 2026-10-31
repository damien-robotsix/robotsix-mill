"""Implement stage: spec emptiness, spawn counter and pre-LLM spawn abort visibility.

Extracted from ``test_implement.py`` (module-size split).
"""

import hashlib
import json
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import ImplementStage
from tests.stages.implement.conftest import (
    _ticket,
    _write_file_map,
    make_bare_repo,
)

# --- spec emptiness precondition ----------------------------------------


def test_empty_spec_blocks_before_agent(ctx_factory, tmp_path, monkeypatch):
    """When the ticket spec is empty, implement blocks BEFORE invoking
    the coordinator agent — no paid re-spawn, no $0.00 trace."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Empty spec ticket", body="")
    _write_file_map(ctx, t, "feature.txt")

    # Track whether the agent was ever invoked.
    agent_called = False

    def _track(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _track)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "spec is empty" in out.note.lower()
    assert not agent_called, "agent must not be invoked for empty spec"


def test_whitespace_only_spec_blocks_before_agent(ctx_factory, tmp_path, monkeypatch):
    """A spec that is only whitespace is treated as empty and blocks."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Whitespace spec", body="\n  \n\t\n")
    _write_file_map(ctx, t, "feature.txt")

    agent_called = False

    def _track(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _track)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "spec is empty" in out.note.lower()
    assert not agent_called


def test_non_empty_spec_proceeds_normally(ctx_factory, tmp_path, monkeypatch):
    """A non-empty spec must still reach the agent (no regression)."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Normal ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    agent_called = False

    def _agent(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("did the thing", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert agent_called, "agent must be invoked for non-empty spec"


# --- implement spawn counter --------------------------------------------


def test_spawn_counter_blocks_after_limit(ctx_factory, tmp_path, monkeypatch):
    """After ``implement_max_spawns_per_ticket`` entries, the preflight
    gate blocks BEFORE a trace opens or the agent is invoked."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    # Seed the counter at the limit — next preflight trips the gate.
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # Also seed a stale conversation state — the block should clear it.
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    conv_state.write_text('{"messages":[]}')

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    # Conversation state must be cleared so a resume starts fresh.
    assert not conv_state.exists()


def test_spawn_counter_increments_each_run(ctx_factory, tmp_path, monkeypatch):
    """Each preflight call increments the spawn counter file only when
    retry_attempt == 0 (genuine re-spawn). Transient retries must not
    burn spawn slots."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="5",
    )
    t = _ticket(ctx, title="Counter ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    _call_count = 0

    def _agent(*, repo_dir, spec, **kwargs):
        nonlocal _call_count
        _call_count += 1
        (Path(repo_dir) / "feature.txt").write_text(f"done round {_call_count}")
        return (f"done round {_call_count}", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    # First invocation: preflight increments counter from 0 to 1
    # (retry_attempt == 0 → genuine re-spawn).
    pre1 = ImplementStage().preflight(t, ctx)
    assert pre1 is None, "preflight should proceed"

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.DOCUMENTING

    ws = ctx.service.workspace(t)
    counter_path = ws.artifacts_dir / "implement_spawn_count"
    assert counter_path.exists()
    assert counter_path.read_text(encoding="utf-8").strip() == "1"

    # Reset the ticket to READY for a second invocation.
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    t = ctx.service.get(t.id)

    # Second invocation: preflight increments counter from 1 to 2.
    pre2 = ImplementStage().preflight(t, ctx)
    assert pre2 is None, "preflight should proceed"

    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.DOCUMENTING

    assert counter_path.read_text(encoding="utf-8").strip() == "2"

    # --- transient retry: counter must NOT increment ---
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    # Simulate a transient retry by setting retry_attempt > 0.
    ctx.service.set_retry_state(
        t.id,
        retry_attempt=2,
        last_transient_error="sandbox EOF",
        next_retry_at=None,
    )
    t = ctx.service.get(t.id)
    assert t.retry_attempt == 2

    pre3 = ImplementStage().preflight(t, ctx)
    assert pre3 is None, "preflight should still proceed on retry"

    # Counter must still be 2 — transient retry does NOT burn a spawn slot.
    assert counter_path.read_text(encoding="utf-8").strip() == "2"


def test_spawn_counter_disabled_when_set_to_zero(ctx_factory, tmp_path, monkeypatch):
    """When ``implement_max_spawns_per_ticket=0`` the counter gate is
    skipped entirely."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="0",
    )
    t = _ticket(ctx, title="Unlimited spawns", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    # Seed a counter at 999 — should be ignored since limit is 0 (disabled).
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("999", encoding="utf-8")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    agent_called = False

    def _agent(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    pre = ImplementStage().preflight(t, ctx)
    assert pre is None, "preflight should proceed when counter is disabled"

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert agent_called, "agent must be invoked when counter is disabled"


def test_spawn_counter_blocked_note_includes_summary_tail(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached, the BLOCKED outcome note
    includes the tail of artifacts/implement_summary.md so the operator
    sees the genuine failure cause."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap with summary", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    summary = (
        "## Implement result\n\n"
        "The agent failed because of a level-3 Claude SDK error:\n"
        "RunContext tools are not available in this environment.\n"
        "Try upgrading the SDK or using a different model tier.\n"
    )
    (ws.artifacts_dir / "implement_summary.md").write_text(summary, encoding="utf-8")
    # Seed a stale conversation state — spawn-limit block must clear it.
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    conv_state.write_text('{"messages":[]}')

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    assert "Last attempt summary tail:" in out.note
    assert "RunContext tools are not available" in out.note
    assert not conv_state.exists()


def test_spawn_counter_blocked_note_no_summary_when_file_missing(ctx_factory, tmp_path):
    """When implement_summary.md does not exist, the BLOCKED note
    does not include a summary tail section."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap no summary", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # No implement_summary.md written.

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    assert "Last attempt summary tail:" not in out.note


def test_spawn_limit_exhausted_emits_diagnostic_event(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached, a SPAWN_LIMIT_EXHAUSTED diagnostic
    event is emitted so agents can discover the exhaustion programmatically."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap event", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    (ws.artifacts_dir / "implement_summary.md").write_text(
        "## Implement result\n\nFailed.\n", encoding="utf-8"
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()

    events = list_diagnostic_events(ctx.settings, "test-board")
    spawn_events = [e for e in events if e.category == "SPAWN_LIMIT_EXHAUSTED"]
    assert len(spawn_events) == 1
    assert spawn_events[0].ticket_id == t.id
    assert "spawn limit reached" in spawn_events[0].reason.lower()
    assert spawn_events[0].normalized_key.startswith("spawn_limit_exhausted:")


def test_recurring_spawn_exhaustion_emits_recurring_event(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached for the second consecutive time
    with an UNCHANGED spec, a RECURRING_SPAWN_EXHAUSTION diagnostic
    event is emitted instead of the plain SPAWN_LIMIT_EXHAUSTED."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events
    from robotsix_mill.core.workspace import record_spawn_exhaustion_marker

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Recurring spawn", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    # Counter at limit.
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # Pre-seed the marker as if the ticket already exhausted once
    # on the same spec fingerprint.  The effective fingerprint for
    # a non-epic ticket is sha256(description)[:16].
    effective_desc = "Add a feature.txt file"
    fp = hashlib.sha256(effective_desc.encode("utf-8")).hexdigest()[:16]
    record_spawn_exhaustion_marker(ws, fp, 1)

    out = ImplementStage().preflight(t, ctx)

    events = list_diagnostic_events(ctx.settings, "test-board")
    recurring_events = [e for e in events if e.category == "RECURRING_SPAWN_EXHAUSTION"]
    assert len(recurring_events) == 1
    assert recurring_events[0].ticket_id == t.id
    assert "unchanged spec" in recurring_events[0].reason.lower()
    assert recurring_events[0].normalized_key.startswith("spawn_limit_exhausted:")
    # Verify no plain event also emitted.
    spawn_events = [e for e in events if e.category == "SPAWN_LIMIT_EXHAUSTED"]
    assert len(spawn_events) == 0

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "counter will not be auto-reset" in out.note.lower()


def test_first_spawn_exhaustion_emits_plain_event(ctx_factory, tmp_path, monkeypatch):
    """When the spawn limit is reached for the first time, a plain
    SPAWN_LIMIT_EXHAUSTED event is emitted (not RECURRING) and the
    block note still mentions auto-reset."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="First spawn", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # No marker pre-seeded — first exhaustion.

    out = ImplementStage().preflight(t, ctx)

    events = list_diagnostic_events(ctx.settings, "test-board")
    spawn_events = [e for e in events if e.category == "SPAWN_LIMIT_EXHAUSTED"]
    assert len(spawn_events) == 1
    recurring_events = [e for e in events if e.category == "RECURRING_SPAWN_EXHAUSTION"]
    assert len(recurring_events) == 0

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "resume-blocked to retry" in out.note.lower()
    assert "clears the counter automatically" in out.note.lower()


def test_spawn_exhaustion_captures_tool_outputs_before_clearing_state(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached and a conversation state exists
    with tool-return parts, the tool outputs are captured to a durable
    artifact BEFORE the conversation state is cleared, and the block
    note includes a tail of those outputs."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Tool capture", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")

    # Write a conversation state JSON with tool-return parts.
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    messages = [
        {
            "parts": [
                {
                    "part_kind": "tool-return",
                    "tool_call_id": "call_1",
                    "content": "exit=1\nruff failed: E501 line too long",
                },
                {
                    "part_kind": "tool-return",
                    "tool_call_id": "call_2",
                    "content": "module-registration: 2 files unclassified",
                },
                {
                    "part_kind": "tool-return",
                    "tool_call_id": "call_3",
                    "content": "No changes detected in working tree.",
                },
            ]
        }
    ]
    conv_state.write_text(json.dumps(messages), encoding="utf-8")

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED

    # The conversation state must be cleared (file deleted).
    assert not conv_state.exists(), "conversation state should be cleared"

    # The durable tool-output artifact must exist.
    artifact = ws.artifacts_dir / "implement_tool_outputs.md"
    assert artifact.exists(), "tool outputs artifact should exist"
    artifact_text = artifact.read_text(encoding="utf-8")
    assert "ruff failed" in artifact_text
    assert "module-registration" in artifact_text
    assert "No changes detected" in artifact_text

    # The block note must reference the tool outputs.
    assert "Last attempt tool outputs:" in out.note
    assert "ruff failed" in out.note or "module-registration" in out.note


def test_spawn_exhaustion_no_conversation_state_is_harmless(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached but no conversation state file
    exists, the exhaustion path proceeds without error and no tool
    output artifact is created."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="No conv state", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # No conversation state file — simulate a case where it was
    # already cleared or never created.

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    # No tool output artifact should be created.
    artifact = ws.artifacts_dir / "implement_tool_outputs.md"
    assert not artifact.exists()


# --- pre-LLM spawn abort visibility (process-death kill recovery) --------


def test_killed_inflight_spawn_is_absorbed_not_counted(
    ctx_factory, tmp_path, monkeypatch
):
    """A stale in-flight marker (previous attempt killed by process
    shutdown) is absorbed before the limit check: the killed attempt
    does NOT burn a spawn slot, so a counter at the limit proceeds."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Killed spawn", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # Stale marker: attempt #1 was killed mid-flight.
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 1, "counted": true}',
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)

    # The killed attempt was absorbed: counter went 1 -> 0, so the
    # limit check passes and preflight proceeds (no block).
    assert out is None, f"killed spawn should not consume a slot: {out}"

    counter = (
        (ws.artifacts_dir / "implement_spawn_count").read_text(encoding="utf-8").strip()
    )
    # Proceeding preflight re-increments: absorbed kill (-1) then new
    # attempt (+1) → back to 1, but the BLOCK did not fire.
    assert counter == "1"

    # The abort was recorded durably.
    aborts = (ws.artifacts_dir / "implement_spawn_aborts.jsonl").read_text(
        encoding="utf-8"
    )
    assert "2026-08-16T20:42:00+00:00" in aborts
    assert '"counted": true' in aborts


def test_killed_inflight_spawn_block_note_carries_evidence(
    ctx_factory, tmp_path, monkeypatch
):
    """When the limit fires despite kill absorption (counter still at
    limit after decrement), the block note carries the kill evidence —
    spawn-limit blocks are never evidence-free."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Killed spawn block", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    # Two counted attempts, one of which was killed mid-flight.
    (ws.artifacts_dir / "implement_spawn_count").write_text("2", encoding="utf-8")
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 2, "counted": true}',
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    # The block note must include the kill evidence.
    assert "killed by process shutdown" in out.note
    assert "2026-08-16T20:42:00+00:00" in out.note


def test_uncounted_killed_spawn_does_not_decrement_counter(
    ctx_factory, tmp_path, monkeypatch
):
    """A killed in-flight marker for an UNCCOUNTED retry (retry_attempt
    > 0) logs the abort but must NOT decrement the spawn counter."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="3",
    )
    t = _ticket(ctx, title="Uncounted kill", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 1, "counted": false}',
        encoding="utf-8",
    )
    # Simulate a transient retry dispatch (retry_attempt > 0): preflight
    # must not increment the counter on this pass.
    ctx.service.set_retry_state(
        t.id,
        retry_attempt=1,
        last_transient_error="sandbox EOF",
        next_retry_at=None,
    )
    t = ctx.service.get(t.id)

    out = ImplementStage().preflight(t, ctx)

    assert out is None, f"counter below limit should proceed: {out}"
    # Counter untouched — the killed retry was not counted.
    assert (ws.artifacts_dir / "implement_spawn_count").read_text(
        encoding="utf-8"
    ).strip() == "1"
    # But the abort is still logged.
    aborts = (ws.artifacts_dir / "implement_spawn_aborts.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"counted": false' in aborts


def test_inflight_marker_cleared_after_run_completes(
    ctx_factory, tmp_path, monkeypatch
):
    """After a successful implement run, the in-flight spawn marker is
    cleared so the next preflight doesn't misread a completed attempt
    as a process-death kill."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="3",
    )
    t = _ticket(ctx, title="Marker cleared", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _agent(*, repo_dir, spec, **kwargs):
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    ws = ctx.service.workspace(t)

    pre = ImplementStage().preflight(t, ctx)
    assert pre is None
    # Marker written by preflight.
    assert (ws.artifacts_dir / "implement_spawn_state.json").exists()

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    # Marker cleared by the run() completion hook.
    assert not (ws.artifacts_dir / "implement_spawn_state.json").exists()


def test_inflight_marker_cleared_after_run_raises(ctx_factory, tmp_path, monkeypatch):
    """When implement run raises (worker records the error durably),
    the marker is still cleared — the attempt reached a recorded
    terminal state and must not be misread as a silent kill."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="3",
    )
    t = _ticket(ctx, title="Marker cleared on raise", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)

    pre = ImplementStage().preflight(t, ctx)
    assert pre is None
    assert (ws.artifacts_dir / "implement_spawn_state.json").exists()

    def _boom(*a, **kw):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(coding, "run_implement_agent", _boom)

    with pytest.raises(RuntimeError, match="agent exploded"):
        ImplementStage().run(t, ctx)

    # Marker cleared even on raise — the worker's error handler records
    # the failure, so the attempt is not silent.
    assert not (ws.artifacts_dir / "implement_spawn_state.json").exists()


def test_resume_blocked_clears_spawn_state_and_aborts(
    ctx_factory, tmp_path, monkeypatch
):
    """resume-blocked at the spawn limit clears the spawn counter AND
    the spawn-state ledger (in-flight marker + abort log) so the
    operator's intervention grants a clean budget."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Resume clears ledger", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 1, "counted": true}',
        encoding="utf-8",
    )
    (ws.artifacts_dir / "implement_spawn_aborts.jsonl").write_text(
        '{"started_at": "2026-08-16T20:42:00+00:00", "spawn_count": 1, '
        '"detected_at": "2026-08-16T20:43:00+00:00", "counted": true}\n',
        encoding="utf-8",
    )

    # Block the ticket from READY (spawn limit) then resume it.
    ctx.service.transition(t.id, State.BLOCKED, "implement spawn limit reached")
    resumed = ctx.service.resume_blocked(t.id, note="operator retry")
    assert resumed.state is State.READY

    assert not (ws.artifacts_dir / "implement_spawn_count").exists()
    assert not (ws.artifacts_dir / "implement_spawn_state.json").exists()
    assert not (ws.artifacts_dir / "implement_spawn_aborts.jsonl").exists()
