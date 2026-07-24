"""Scope-guardrail + preflight integrity tests for the implement stage.

Extracted from ``test_implement.py`` (2026-07-21 module-size split).
Covers:
- Scope enforcement (file_map gating — violation, pass, skip, directory prefixes)
- Scope-triage integration (EXPAND, REJECT, ESCALATE, disabled, agent error)
- Preflight checks (tool-definition, skill-file, workspace integrity)
- Transient vs spec-determined error fingerprinting
- Stale re-spawn guard
- Stuck-detection backstops (no-diff passes, cumulative tool calls)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.implement import ImplementStage


# --- fixtures / helpers (copied from tests/stages/implement/test_implement.py) ---


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_bare_repo(tmp_path: Path) -> str:
    """A throwaway local remote (file://) with a `main` branch — lets us
    exercise clone/branch/commit fully offline, no forge."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", "main")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    created = []

    def make(**env):
        db.reset_engine()
        # fake_sandbox replaces the (always-containerized) seam; no
        # Docker, no host execution.
        s = Settings(data_dir=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        created.append(s)
        from robotsix_mill.config import RepoConfig

        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


def _ticket(ctx, title="Add feature", body="Please add feature.txt"):
    t = ctx.service.create(title, body)
    ctx.service.transition(t.id, State.READY)
    return ctx.service.get(t.id)


def _write_file_map(ctx, ticket, *files):
    """Write a minimal file_map.json for *ticket* listing *files*."""
    import json as _json

    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "file_map.json").write_text(
        _json.dumps([{"file": f, "note": "test"} for f in files]),
        encoding="utf-8",
    )


def _fake_agent(write: dict | None):
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
        )  # signature must match the seam
        if write:
            for name, content in write.items():
                (Path(repo_dir) / name).write_text(content)
        return (
            "did the thing",
            list(write.keys()) if write else [],
            "",
            None,
            None,
            False,
            "",
        )

    return _run


# --- scope guardrail ----------------------------------------------------


def test_scope_violation_blocks_ticket(ctx_factory, tmp_path, monkeypatch):
    """When the agent modifies a tracked file not in file_map, the scope
    check catches it and immediately blocks the ticket — no retry.
    The test gate is never reached on the violating iteration."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
        scope_triage_enabled="false",
    )
    t = _ticket(ctx)

    # file_map only allows wip.txt — README.md is out of scope.
    ws = ctx.service.workspace(t)
    file_map_path = ws.artifacts_dir / "file_map.json"
    file_map_path.write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    call_count = {"n": 0}

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
        call_count["n"] += 1
        # Write wip.txt (in-scope) AND modify README.md (out-of-scope)
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "scope violation" in out.note
    assert call_count["n"] == 1, "agent must not be retried"
    assert (ctx.service.workspace(t).artifacts_dir / "implement.md").exists()


def test_scope_check_passes_when_all_in_scope(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """When every changed file is in file_map, the scope check passes,
    logs an info message, and the loop proceeds to the test gate."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    # file_map includes wip.txt → scope check should pass.
    ws = ctx.service.workspace(t)
    file_map_path = ws.artifacts_dir / "file_map.json"
    file_map_path.write_text(
        '[{"file": "wip.txt", "note": "the change"}]',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        _fake_agent({"wip.txt": "done"}),
    )
    import logging

    with caplog.at_level(logging.INFO, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert any(
        "scope check passed" in m and "1 file(s) changed" in m for m in caplog.messages
    ), f"expected scope-passed info log, got: {caplog.messages}"


def test_scope_check_directory_entry_prefix_matches(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """A file_map entry ending in "/" covers every file under that
    directory — regression for auto-mail 6624, where a declared
    ".deps/" removal flooded the scope check with 118 "out-of-scope"
    files that were the ticket's own deliverable."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    file_map_path = ws.artifacts_dir / "file_map.json"
    file_map_path.write_text(
        '[{"file": "vendored/", "note": "delete the vendored tree"}]',
        encoding="utf-8",
    )

    inner = _fake_agent({"vendored/pkg/data.txt": "x", "vendored/other.txt": "y"})

    def _agent_with_dirs(*, repo_dir, **kwargs):
        (Path(repo_dir) / "vendored" / "pkg").mkdir(parents=True, exist_ok=True)
        return inner(repo_dir=repo_dir, **kwargs)

    monkeypatch.setattr(coding, "run_implement_agent", _agent_with_dirs)
    import logging

    with caplog.at_level(logging.INFO, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert any("scope check passed" in m for m in caplog.messages), (
        f"expected scope-passed info log, got: {caplog.messages}"
    )


def test_scope_check_skipped_when_no_file_map(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """When file_map.json is absent, the stage logs a warning and
    proceeds — scope enforcement is skipped, not blocked."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)
    # No file_map.json written → stage should warn and proceed.

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
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import logging

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING, (
        f"expected DOCUMENTING, got {out.next_state}"
    )
    assert len(agent_called) == 1, "agent must be called when file_map is missing"
    assert any("skipping scope enforcement" in m for m in caplog.messages), (
        f"expected scope-skip warning, got: {caplog.messages}"
    )


def test_scope_check_skipped_when_file_map_empty(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """When file_map.json exists but is an empty array, the stage logs
    a warning and proceeds — same as a missing file_map."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text("[]", encoding="utf-8")

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
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import logging

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING, (
        f"expected DOCUMENTING, got {out.next_state}"
    )
    assert len(agent_called) == 1, "agent must be called when file_map is empty"
    assert any("skipping scope enforcement" in m for m in caplog.messages), (
        f"expected scope-skip warning, got: {caplog.messages}"
    )


# --- scope-triage integration tests -------------------------------------


def test_scope_triage_expand_continues_loop(ctx_factory, tmp_path, monkeypatch):
    """EXPAND verdict: file_map is updated in-memory, the loop continues,
    and a comment is posted.  When at least one expand-file has *not*
    been modified yet, the agent is re-run (no retroactive short-circuit)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    call_count = {"n": 0}

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
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="Minor dependency edit is a legitimate consequence",
            # CHANGELOG.md is *not* in the diff, so there is genuinely
            # new work to do → loop MUST continue.
            expand_files=["README.md", "CHANGELOG.md"],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    # With at least one expand-file not yet modified, the loop must continue
    # (agent called at least twice).
    assert call_count["n"] >= 2, "EXPAND with unmodified file should continue the loop"
    assert out.next_state is not State.BLOCKED
    # The EXPAND decision lands in history, not comments (v1).
    history = ctx.service.history(t.id)
    assert any((ev.note or "").startswith("scope-triage EXPAND") for ev in history)
    comments = ctx.service.list_comments(t.id)
    assert not any("[scope-triage]" in (c.body or "") for c in comments), (
        "scope-triage no longer emits comments — it uses add_step_event"
    )


def test_scope_triage_expand_retroactive_short_circuit(
    ctx_factory, tmp_path, monkeypatch
):
    """When scope-triage EXPANDs files that are *all* already in the
    current diff, the retroactive short-circuit fires: the agent is NOT
    re-run, the loop falls through to the test gate, and (with tests
    passing) the ticket finalizes without wasting an iteration."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    call_count = {"n": 0}

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
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("agent summary text", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="README.md is a natural side-effect edit",
            expand_files=["README.md"],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    # Agent must be called exactly once (no wasted re-run).
    assert call_count["n"] == 1, "retroactive short-circuit should skip agent re-run"
    assert out.next_state is State.DOCUMENTING, (
        f"expected DOCUMENTING, got {out.next_state}"
    )
    # The EXPAND decision lands in history (v1 — no more comments).
    # The agent summary also lands in history (as a same-state
    # `implement:` step event, post the implement-step-event change).
    # The transition note is now a short stage-name marker, not the
    # summary.
    history = ctx.service.history(t.id)
    assert any((ev.note or "").startswith("scope-triage EXPAND") for ev in history)
    assert any(
        (ev.note or "").startswith("implement:")
        and "agent summary text" in (ev.note or "")
        for ev in history
    )


def test_scope_triage_reject_to_ready(ctx_factory, tmp_path, monkeypatch):
    """REJECT verdict: ticket goes back to READY with a comment naming
    the rogue files."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

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
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="REJECT",
            justification="Unrelated module — scope creep",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "REJECT" in out.note
    # The REJECT details live in the transition note (history) — not
    # in comments. Files are quoted in backticks so the dedup loop
    # can scan history events.
    assert "README.md" in (out.note or "")
    comments = ctx.service.list_comments(t.id)
    assert not any("scope-triage" in (c.body or "") for c in comments)


def test_scope_triage_escalate_to_blocked(ctx_factory, tmp_path, monkeypatch):
    """ESCALATE verdict: ticket goes to BLOCKED with triage reasoning."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

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
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="ESCALATE",
            justification="Ambiguous spec — cannot classify",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "ESCALATE" in out.note
    # ESCALATE reasoning + the out-of-scope file list now live in the
    # transition note rather than a comment.
    assert "README.md" in (out.note or "")
    comments = ctx.service.list_comments(t.id)
    assert not any("scope-triage" in (c.body or "") for c in comments)


def test_scope_triage_disabled_falls_through(ctx_factory, tmp_path, monkeypatch):
    """When scope_triage_enabled=False, existing BLOCKED behaviour is
    preserved exactly — no triage agent is called."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
        scope_triage_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    call_count = {"n": 0}

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
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "scope violation" in out.note
    assert call_count["n"] == 1, "agent must not be retried"


def test_scope_triage_agent_error_escalates(ctx_factory, tmp_path, monkeypatch):
    """When the triage agent raises an exception, the ticket escalates
    to BLOCKED with an agent-error note."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

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
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    def _failing_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _failing_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "agent error" in out.note
    # The note names the ACTUAL exception — a bare "agent error" reads
    # like a scope verdict and sends the operator log-hunting.
    assert "RuntimeError: model unavailable" in out.note
    # The "agent error" diagnostic lives in the transition note now
    # (v1 — scope-triage no longer comments).
    comments = ctx.service.list_comments(t.id)
    assert not any("scope-triage" in (c.body or "") for c in comments)


# --- preflight: tool-definition integrity ---------------------------------


def test_preflight_blocks_when_agent_definition_has_empty_tools(
    ctx_factory, tmp_path, monkeypatch
):
    """When the implement agent definition declares no tools, preflight
    must block BEFORE opening a trace — an agent with no tools is a
    guaranteed no-op."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Empty tools", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    from robotsix_mill.agents.yaml_loader import AgentDefinition

    empty_def = AgentDefinition(
        name="implement",
        level=2,
        system_prompt="you are a bot",
        tools=[],  # empty tools list
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.implement.phase_coordinator.load_agent_definition",
        lambda _path: empty_def,
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "no tools configured" in out.note.lower()


def test_preflight_blocks_when_agent_definition_fails_to_load(
    ctx_factory, tmp_path, monkeypatch
):
    """When the implement agent definition YAML cannot be loaded
    (missing file, invalid YAML), preflight must block with the
    underlying error."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Bad def", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.phase_coordinator.load_agent_definition",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("no such file")),
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "failed to load implement agent definition" in out.note.lower()
    assert "no such file" in out.note


# --- preflight: skill-file integrity --------------------------------------


def test_preflight_blocks_when_skill_file_missing(ctx_factory, tmp_path, monkeypatch):
    """When a skill referenced by the agent definition does not exist
    on disk, preflight must block with the missing path in the note."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Missing skill", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Point skills_dir at an empty temp directory so no skill files
    # exist.
    empty_skills = tmp_path / "no_skills_here"
    empty_skills.mkdir()
    monkeypatch.setattr(ctx.settings, "skills_dir", empty_skills)

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "missing skill file:" in out.note.lower()
    assert "SKILL.md" in out.note


# --- preflight: workspace integrity ---------------------------------------


def test_preflight_blocks_when_workspace_absent(ctx_factory, tmp_path, monkeypatch):
    """When the ticket workspace directory has been deleted (or the
    filesystem is inaccessible), preflight must block with the
    workspace path in the note."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        # Disable checks that access ws.artifacts_dir (which would
        # recreate the workspace directory as a side-effect).
        implement_max_spawns_per_ticket="0",
        max_implement_review_cycles="0",
    )
    t = _ticket(ctx, title="No workspace", body="Implement feature X")

    import robotsix_mill.core.workspace as wmod

    # Delete the workspace directory that _ticket (via create) made.
    # Then monkeypatch Workspace so preflight's own workspace() call
    # doesn't recreate it, and stub read_description so the
    # spec-empty gate passes.
    import shutil

    ws_for_deletion = ctx.service.workspace(t)
    shutil.rmtree(ws_for_deletion.dir)

    def _no_mkdir(self, root, ticket_id):
        from pathlib import Path
        import os

        if ticket_id != Path(ticket_id).name or ticket_id in (".", ".."):
            raise ValueError(f"Unsafe ticket_id: {ticket_id!r}")
        _root = os.path.realpath(os.fspath(root))
        _dir = os.path.realpath(os.path.join(_root, ticket_id))
        if not _dir.startswith(_root + os.sep):
            raise ValueError(f"Unsafe ticket_id: {ticket_id!r}")
        self.dir = Path(_dir)

    monkeypatch.setattr(wmod.Workspace, "__init__", _no_mkdir)

    # Prevent artifacts_dir from recreating the directory.
    monkeypatch.setattr(
        wmod.Workspace,
        "artifacts_dir",
        property(lambda self: self.dir / "artifacts"),
    )

    # Stub read_description so the spec-empty gate passes.
    monkeypatch.setattr(
        wmod.Workspace, "read_description", lambda self: "Implement feature X"
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "workspace directory absent or inaccessible" in out.note.lower()


def test_preflight_blocks_when_language_instructions_dir_absent(
    ctx_factory, tmp_path, monkeypatch
):
    """When neither the configured language_instructions_dir nor the
    packaged fallback exists, preflight must block with the path in the
    note."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Missing lang dir", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Point language_instructions_dir at a non-existent path AND make the
    # packaged fallback unresolvable, so the check genuinely fails.
    missing = tmp_path / "no_lang_instructions_here"
    monkeypatch.setattr(ctx.settings, "language_instructions_dir", missing)
    monkeypatch.setattr(
        "robotsix_mill._resources.language_instructions_dir",
        lambda: tmp_path / "no_packaged_copy_either",
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "language_instructions_dir" in out.note.lower()


def test_preflight_missing_language_dir_falls_back_to_packaged(
    ctx_factory, tmp_path, monkeypatch
):
    """A stale CWD-relative override (2026-07-19 incident) must NOT block
    when the packaged snippets exist — the loader falls back to them."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Stale lang dir override", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    missing = tmp_path / "no_lang_instructions_here"
    monkeypatch.setattr(ctx.settings, "language_instructions_dir", missing)

    out = ImplementStage().preflight(t, ctx)

    assert out is None or "language_instructions_dir" not in (out.note or "").lower(), (
        f"language check must not block: {out.note!r}"
    )


def test_convergence_backstop_writes_implement_md(ctx_factory, tmp_path, monkeypatch):
    """When the convergence backstop fires (empty diff after review) it
    now terminates DONE (already satisfied). implement.md must still be
    written (with the passed marker + spec-fingerprint) so the artifact
    trail is intact."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="true",
        max_implement_review_cycles="10",
    )
    t = _ticket(ctx, title="Convergence ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Run implement once so a branch and commits exist.
    def _agent(*, repo_dir, spec, **kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.CODE_REVIEW

    # Simulate returning from review with no new commits.
    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    branch = f"{ctx.settings.branch_prefix}{t.id}"
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", "origin/main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", "-B", branch],
        check=True,
        capture_output=True,
    )

    t = ctx.service.get(t.id)
    assert t.review_rounds == 1

    # Second run: convergence backstop fires → DONE (already satisfied).
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.DONE
    assert "already satisfied" in out2.note.lower()

    # implement.md must have been written with the passed outcome.
    md = ws.artifacts_dir / "implement.md"
    assert md.exists(), "convergence backstop must write implement.md"
    content = md.read_text(encoding="utf-8")
    assert "passed" in content
    assert "spec-fingerprint:" in content


def test_transient_agent_error_does_not_persist_fingerprint(
    ctx_factory, tmp_path, monkeypatch
):
    """When the implement agent raises AgentRunError with a transient
    cause (simulating a network blip), _finalize must NOT be called,
    so no fingerprint is persisted.  The next pass must re-implement
    normally instead of hitting the stale-re-spawn guard."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transient blip", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    from robotsix_mill.agents.coding import AgentRunError

    # Simulate a transient error: AgentRunError wrapping an
    # httpx.ConnectError (classified as transient by
    # classify_stage_error).
    cause = None
    try:
        import httpx

        cause = httpx.ConnectError("Connection refused")
    except ImportError:
        cause = ConnectionError("Connection refused")

    def _agent_transient(*, repo_dir, spec, **kwargs):
        raise AgentRunError("transient blip", messages=[], cause=cause)

    monkeypatch.setattr(coding, "run_implement_agent", _agent_transient)

    # The implement stage should raise the transient cause, NOT
    # return a normal Outcome.
    with pytest.raises(Exception):  # noqa: B017
        ImplementStage().run(t, ctx)

    # implement.md must NOT exist (or must not have a fingerprint)
    # because _finalize was never called for this transient error.
    ws = ctx.service.workspace(t)
    implement_md = ws.artifacts_dir / "implement.md"
    if implement_md.exists():
        content = implement_md.read_text(encoding="utf-8")
        assert "spec-fingerprint:" not in content, (
            "transient error must not persist a spec fingerprint"
        )


def test_spec_determined_error_persists_fingerprint(ctx_factory, tmp_path, monkeypatch):
    """When the implement agent raises AgentRunError with a NON-transient
    cause (a genuine spec/logic dead-end), _finalize IS called and the
    fingerprint IS persisted so the stale-re-spawn guard can block a
    re-run with an unchanged spec."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Hard error", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    from robotsix_mill.agents.coding import AgentRunError

    # A ValueError is NOT classified as transient — it's a spec-level
    # dead-end (the agent couldn't make sense of the spec).
    cause = ValueError("cannot parse specification")

    def _agent_fatal(*, repo_dir, spec, **kwargs):
        raise AgentRunError("fatal spec error", messages=[], cause=cause)

    monkeypatch.setattr(coding, "run_implement_agent", _agent_fatal)

    out = ImplementStage().run(t, ctx)
    # Non-transient → BLOCKED outcome with fingerprint persisted.
    assert out.next_state is State.BLOCKED

    ws = ctx.service.workspace(t)
    implement_md = ws.artifacts_dir / "implement.md"
    assert implement_md.exists(), "spec-determined error must write implement.md"
    content = implement_md.read_text(encoding="utf-8")
    assert "spec-fingerprint:" in content, (
        "spec-determined error must persist a spec fingerprint"
    )
    assert "BLOCKED — resumable" in content


def test_stale_respawn_guard_allows_after_transient(ctx_factory, tmp_path, monkeypatch):
    """When implement.md has no fingerprint (written by a pre-fix
    transient attempt, or the fingerprint was omitted), preflight
    must NOT block — it must allow the re-spawn."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transient resume", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    # Write implement.md with BLOCKED status but NO fingerprint line
    # (simulating a transient-failure artifact from before the fix
    # was applied, or a fresh transient attempt).
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        "\nno changes produced (transient abort)\n",
        encoding="utf-8",
    )

    # Preflight should NOT block — no fingerprint means transient.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when fingerprint is absent, got: {out}"


def test_stuck_no_diff_passes_aborts_loop(ctx_factory, tmp_path, monkeypatch):
    """After N consecutive passes with no file edits, the loop aborts
    with a 'stuck' BLOCKED instead of exhausting max_fix_iterations."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="false",  # gate always fails → retry
        max_fix_iterations="8",  # high enough that stuck fires first
    )
    call_count = [0]

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
        call_count[0] += 1
        # Produce NO file edits — the agent only reads / explores.
        # Return new_msgs that simulate a stuck read_ticket loop.
        import json

        msgs = json.dumps(
            [
                {
                    "parts": [
                        {
                            "part_kind": "tool-call",
                            "tool_name": "read_ticket",
                            "args": {},
                            "tool_call_id": f"call_rt_{call_count[0]}",
                        }
                    ]
                }
            ]
        ).encode()
        return (f"attempt {call_count[0]}", [], "", None, msgs, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "stuck" in out.note.lower()
    # Should abort at _STUCK_NO_DIFF_PASSES (3) + 1, not at max_fix_iterations (8).
    assert call_count[0] <= 4  # 3 no-diff passes + maybe the initial


def test_stuck_cumulative_tool_calls_aborts_loop(ctx_factory, tmp_path, monkeypatch):
    """After M cumulative tool calls across passes with no git diff,
    the loop aborts with a 'stuck' BLOCKED."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="false",  # gate always fails → retry
        max_fix_iterations="8",
    )
    call_count = [0]

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
        call_count[0] += 1
        # Produce NO file edits but simulate HEAVY tool usage (many
        # read_ticket calls per pass) so the cumulative cap fires
        # before _STUCK_NO_DIFF_PASSES.
        import json

        parts = []
        for i in range(30):  # 30 tool calls per pass
            parts.append(
                {
                    "part_kind": "tool-call",
                    "tool_name": "read_ticket",
                    "args": {},
                    "tool_call_id": f"call_rt_{call_count[0]}_{i}",
                }
            )
        msgs = json.dumps([{"parts": parts}]).encode()
        return (f"heavy pass {call_count[0]}", [], "", None, msgs, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "stuck" in out.note.lower()
    assert "cumulative tool calls" in out.note.lower()
    # 30 calls/pass, cap at 50 → fires on second pass.
    assert call_count[0] <= 3


# --- stale re-spawn guard (spec-fingerprint) ----------------------------


def test_stale_respawn_guard_blocks_on_matching_fingerprint(
    ctx_factory, tmp_path, monkeypatch
):
    """When implement.md has BLOCKED — resumable status AND a stored
    spec-fingerprint that matches the current effective spec hash,
    preflight must block to prevent wasteful re-implementation."""
    import hashlib

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Guard block", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Compute the effective spec fingerprint the same way
    # preflight and _finalize do.
    spec = ctx.service.workspace(t).read_description() or ""
    # ticket has no parent_id, so effective == spec
    effective = spec
    current_fp = hashlib.sha256(effective.encode("utf-8")).hexdigest()[:16]

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {current_fp}\n"
        "summary-fingerprint: deadbeef00000001\n"
        "stall-count: 0\n"
        "\nprior attempt failed\n",
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)
    assert out is not None, "must block when fingerprint matches"
    assert out.next_state is State.BLOCKED
    assert "spec unchanged" in out.note.lower()


def test_stale_respawn_guard_allows_on_different_fingerprint(
    ctx_factory, tmp_path, monkeypatch
):
    """When implement.md has BLOCKED — resumable status but the stored
    fingerprint differs from the current effective spec, preflight
    must allow — the spec has changed so re-implementation may
    produce a different result."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Guard allow", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        "spec-fingerprint: completely_different_hash\n"
        "summary-fingerprint: deadbeef00000001\n"
        "stall-count: 0\n"
        "\nprior attempt failed\n",
        encoding="utf-8",
    )

    # Preflight must allow — different fingerprint means spec changed.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when fingerprint differs, got: {out}"


def test_stale_respawn_guard_allows_without_fingerprint(
    ctx_factory, tmp_path, monkeypatch
):
    """When implement.md has BLOCKED — resumable status but NO
    spec-fingerprint line, preflight must allow — this is a
    transient or pre-condition failure, not a real implement
    attempt."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="No fingerprint", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        "summary-fingerprint: deadbeef00000001\n"
        "stall-count: 0\n"
        "\nbaseline check failed\n",
        encoding="utf-8",
    )

    # Preflight must allow — no fingerprint means the prior BLOCKED
    # was not a spec-determined implement outcome.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when fingerprint is absent, got: {out}"
