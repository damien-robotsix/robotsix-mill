"""Implement stage: epic context injection and post-edit reference_files persistence.

Extracted from ``test_implement.py`` (module-size split).
"""

import json
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

# --- epic context -------------------------------------------------------


def test_epic_context_prepended_to_spec(ctx_factory, tmp_path, monkeypatch):
    """When a ticket has an epic parent, the spec passed to
    run_implement_agent starts with the epic context wrapper."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",  # this test asserts the no-review path
    )

    # Create an epic with rich global context
    epic = ctx.service.create(
        "Global Epic", "High-level goal: unify UX", kind=TicketKind.EPIC
    )
    # Create a child ticket under this epic
    child = ctx.service.create(
        "Add dark mode",
        "Please add dark mode toggle",
        parent_id=epic.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(child, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(seen_spec) == 1
    expected = (
        "````epic-context\nHigh-level goal: unify UX\n````\n<!-- /epic-context -->"
    )
    assert seen_spec[0].startswith(expected)


def test_epic_context_not_injected_without_epic_parent(
    ctx_factory, tmp_path, monkeypatch
):
    """Ticket without a parent: no epic context in spec."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    t = _ticket(ctx, title="Standalone", body="Just a task")
    _write_file_map(ctx, t, "feature.txt")
    seen_spec: list[str] = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(t, ctx)
    assert len(seen_spec) == 1
    assert "````epic-context" not in seen_spec[0]


def test_epic_context_not_injected_for_non_epic_parent(
    ctx_factory, tmp_path, monkeypatch
):
    """Ticket with a parent that is NOT an epic: no epic context."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    # Create a regular task parent (kind=TicketKind.TASK)
    parent = ctx.service.create("Parent task", "Ordinary task", kind=TicketKind.TASK)
    child = ctx.service.create(
        "Child of task",
        "Do a sub-thing",
        parent_id=parent.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(child, ctx)
    assert len(seen_spec) == 1
    assert "````epic-context" not in seen_spec[0]


def test_epic_context_not_injected_for_empty_epic_description(
    ctx_factory, tmp_path, monkeypatch
):
    """Epic with empty description: no injection."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    epic = ctx.service.create("Empty Epic", "", kind=TicketKind.EPIC)
    child = ctx.service.create(
        "Child of empty epic",
        "Do a thing",
        parent_id=epic.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(child, ctx)
    assert len(seen_spec) == 1
    assert "````epic-context" not in seen_spec[0]


# --- post-edit reference_files persistence ------------------------------


def test_post_edit_reference_files_persisted(ctx_factory, tmp_path, monkeypatch):
    """After a successful agent pass, reference_files.json (paths-only,
    sourced from agent-curated list) and implement_summary.md are written
    to artifacts_dir."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
            previous_attempt_summary,
        )
        agent_called.append(1)
        # Agent edits a file AND curates a list that includes an
        # additional file it didn't touch on disk — curated, not
        # git-derived.
        (Path(repo_dir) / "wip.txt").write_text("post-edit content here")
        return (
            "agent summary text",
            ["wip.txt", "base_class.py"],
            "",
            None,
            None,
            False,
            "",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt", "base_class.py")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1

    artifacts = ctx.service.workspace(t).artifacts_dir

    # reference_files.json exists, paths-only, with agent-curated list.
    ref_path = artifacts / "reference_files.json"
    assert ref_path.exists(), "reference_files.json should exist"
    ref_data = json.loads(ref_path.read_text(encoding="utf-8"))
    assert len(ref_data) == 2
    assert ref_data[0] == {"path": "wip.txt"}
    assert ref_data[1] == {"path": "base_class.py"}

    # implement_summary.md exists with the agent's summary.
    summary_path = artifacts / "implement_summary.md"
    assert summary_path.exists(), "implement_summary.md should exist"
    assert summary_path.read_text(encoding="utf-8") == "agent summary text"


def test_reference_files_reloaded_on_retry(ctx_factory, tmp_path, monkeypatch):
    """On a retry iteration, the reference_files passed to
    run_implement_agent contain the prior pass's agent-curated paths
    (paths-only, reloaded from disk)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="false",
        review_enabled="false",
        max_fix_iterations="2",
    )

    captured_refs: list[list[dict] | None] = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            feedback,
            message_history,
            memory,
            epic_workspace_path,
            previous_attempt_summary,
        )
        captured_refs.append(reference_files)
        (Path(repo_dir) / "wip.txt").write_text("post-edit pass content")
        return ("agent summary", ["wip.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)
    # Test gate always fails → should escalate after 2 iterations.
    assert out.next_state is State.BLOCKED
    assert len(captured_refs) == 2, "agent should be called twice"

    # Second call's reference_files should contain paths-only from
    # the prior pass's agent-curated list.
    refs2 = captured_refs[1]
    assert refs2 is not None, "second call should receive reference_files"
    assert len(refs2) == 1
    assert refs2[0] == {"path": "wip.txt"}


def test_summary_included_in_retry_feedback(ctx_factory, tmp_path, monkeypatch):
    """On a retry iteration, the previous_attempt_summary is threaded to
    the agent alongside the test failure diagnosis as feedback."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="false",
        review_enabled="false",
        max_fix_iterations="2",
    )

    captured_feedback: list[str | None] = []
    captured_prev_summaries: list[str | None] = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        captured_feedback.append(feedback)
        captured_prev_summaries.append(previous_attempt_summary)
        (Path(repo_dir) / "wip.txt").write_text("edited")
        return ("pass-1-summary-abc", ["wip.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert len(captured_feedback) == 2

    # First call: feedback should be None, no previous_attempt_summary.
    assert captured_feedback[0] is None
    assert captured_prev_summaries[0] is None

    # Second call: feedback should be the test-failure diagnosis.
    fb = captured_feedback[1]
    assert fb is not None
    # The diag is from the test agent (sandbox unavailable or test failure)
    assert "sandbox unavailable" in fb.lower() or "fail" in fb.lower()

    # previous_attempt_summary is threaded from implement_summary.md
    assert captured_prev_summaries[1] is not None
    assert "pass-1-summary-abc" in captured_prev_summaries[1]


def test_persistence_without_file_map_still_writes(ctx_factory, tmp_path, monkeypatch):
    """When file_map.json is absent, agent-curated reference_files and
    summary are still persisted — no crash."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        agent_called.append(1)
        (Path(repo_dir) / "out.txt").write_text("done")
        return ("summary", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    # Deliberately do NOT write file_map.json.

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1

    artifacts = ctx.service.workspace(t).artifacts_dir
    # Agent-curated artifacts are still written even without file_map.
    ref_path = artifacts / "reference_files.json"
    assert ref_path.exists(), (
        "reference_files.json should exist from agent-curated list"
    )

    summary_path = artifacts / "implement_summary.md"
    assert summary_path.exists(), "implement_summary.md should exist from agent summary"
