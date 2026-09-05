import json
import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import ImplementStage
from robotsix_mill.vcs import git_ops
from tests.stages.implement.conftest import _ticket, _write_file_map
from tests.stages.implement.test_implement import (
    _clone_repo_to,
    _fake_agent,
    _git,
    make_bare_repo,
)

# --- unit tests for _run_scope_guardrail --------------------------------


def test_run_scope_guardrail_triage_disabled_blocks(ctx_factory, tmp_path, monkeypatch):
    """scope_triage_enabled=False: any out-of-scope file → BLOCKED outcome."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        scope_triage_enabled="false",
    )
    t = _ticket(ctx)
    # file_map only allows "a.txt"
    _write_file_map(ctx, t, "a.txt")

    # Write out-of-scope file to the repo so git_ops.changed_files
    # sees it as a change from origin/main.
    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    (repo / "b.txt").write_text("out of scope")
    # Commit so that changed_files detects it against origin/main
    # (changed_files uses diff between HEAD and origin/<target>).
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "wip")
    # Write file_map.json so the guardrail has a scope to enforce.
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]',
        encoding="utf-8",
    )
    settings = ctx.settings

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "scope violation" in result.outcome.note
    assert "b.txt" in result.outcome.note


def test_run_scope_guardrail_dedup_guard_suppresses_duplicate_reject(
    ctx_factory,
    tmp_path,
    monkeypatch,
):
    """When all out-of-scope files were already REJECTed in prior history
    events, the dedup guard fires → skip_iteration WITHOUT shipping: the
    re-created files are cleaned from the tree and NOT added to file_map.
    v1: the source of truth for the REJECT seed is a step event, not
    a comment (scope-triage no longer comments)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    # Seed a prior scope-triage REJECT history event naming b.txt.
    ctx.service.add_step_event(
        t.id,
        "scope-triage REJECT: prior run — out-of-scope: `b.txt`",
    )

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    (repo / "b.txt").write_text("out of scope again")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "wip")
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]',
        encoding="utf-8",
    )
    settings = ctx.settings

    # Mock the scope-triage agent to return REJECT (the dedup guard
    # should intercept before this matters, but the agent is called).
    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="REJECT",
            justification="Still out of scope",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "skip_iteration"
    # The dedup guard must NOT ship the re-created file: b.txt stays out
    # of file_map and is cleaned back out of the working tree.
    assert result.file_map is not None
    assert "b.txt" not in result.file_map
    assert result.feedback is None
    assert "b.txt" not in git_ops.changed_files(repo, "main")
    assert not (repo / "b.txt").exists()


def _reject_triage(
    *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
):
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    return ScopeTriageVerdict(
        action="REJECT",
        justification="Unrelated scope creep",
        expand_files=[],
    )


def test_run_scope_guardrail_reject_cleans_tracked_and_untracked(
    ctx_factory, tmp_path, monkeypatch
):
    """A first-time REJECT removes the out-of-scope changes from the tree
    before finalize commits: a tracked modification (restored to origin),
    a newly-added tracked file, and an untracked file are all absent from
    the diff vs origin afterwards, while the in-scope change survives."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    # In-scope change + out-of-scope (tracked-mod README.md, new vendored.py),
    # both WIP-committed; plus an untracked stray.txt.
    (repo / "a.txt").write_text("in scope")
    (repo / "README.md").write_text("out of scope edit")
    (repo / "vendored.py").write_text("vendored tree")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip")
    (repo / "stray.txt").write_text("untracked stray")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]', encoding="utf-8"
    )

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _reject_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=ctx.settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "return"
    assert result.outcome.next_state is State.READY
    changed = git_ops.changed_files(repo, "main")
    # Out-of-scope paths gone from the diff (unstaged + WIP-committed).
    assert "README.md" not in changed
    assert "vendored.py" not in changed
    assert "stray.txt" not in changed
    assert not (repo / "vendored.py").exists()
    assert not (repo / "stray.txt").exists()
    # In-scope work preserved.
    assert "a.txt" in changed
    # A REJECT is a send-back with new feedback, not a spec-determined dead
    # end: no spec fingerprint may be persisted, or preflight blocks the
    # very re-run the READY outcome asks for ("spec unchanged since last
    # spec-determined implement attempt").
    implement_md = (ws.artifacts_dir / "implement.md").read_text(encoding="utf-8")
    assert "spec-fingerprint:" not in implement_md


def test_run_scope_guardrail_reject_cleans_resumed_wip_history(
    ctx_factory, tmp_path, monkeypatch
):
    """Resumed-branch case: the rejected file is already in the branch's
    committed history (no unstaged edit). REJECT must scrub it from the
    committed diff vs origin/main, not just the working tree."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    # Simulate a prior polluted WIP commit (in-scope + vendored tree).
    (repo / "a.txt").write_text("in scope")
    (repo / "vendored.py").write_text("vendored tree")
    git_ops.commit_all(repo, "prior wip [WIP]")
    # Re-checkout to mimic a fresh resume off the committed branch.
    git_ops.checkout(repo, branch)
    assert "vendored.py" in git_ops.changed_files(repo, "main")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]', encoding="utf-8"
    )

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _reject_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=ctx.settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "return"
    # finalize committed the cleaned tree → no net committed diff for the
    # rejected file vs origin/main.
    net = subprocess.run(
        ["git", "-C", str(repo), "diff", "origin/main...HEAD", "--name-only"],
        capture_output=True,
        text=True,
    ).stdout
    assert "vendored.py" not in net
    assert "a.txt" in net


# --- .robotsix-mill/config.yaml write-path guard --------------------------


def _seed_repo_settings(ctx, remote, t, base_config: str):
    """Clone, commit *base_config* as .robotsix-mill/config.yaml on
    origin/main, then check out a fresh mill branch. Returns (repo, branch).
    """
    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    (repo / ".robotsix-mill").mkdir(parents=True, exist_ok=True)
    (repo / ".robotsix-mill" / "config.yaml").write_text(base_config)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed repo settings")
    _git(repo, "push", "origin", "main")
    branch = f"mill/{t.id}"
    _git(repo, "checkout", "-q", "-b", branch)
    return repo, branch


def _no_triage(monkeypatch):
    import robotsix_mill.agents.scope_triage as scope_triage_mod

    monkeypatch.setattr(
        scope_triage_mod,
        "run_scope_triage_agent",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("scope-triage must not be called for this case")
        ),
    )


def test_repo_settings_guard_valid_in_scope_edit_proceeds(
    ctx_factory, tmp_path, monkeypatch
):
    """(a) An in-scope, well-formed edit that ADDS extra_sandbox_packages
    while preserving the existing keys → guard returns None, ticket
    proceeds (no block)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ".robotsix-mill/config.yaml")

    repo, branch = _seed_repo_settings(
        ctx, remote, t, "test_command: pytest -q\nlanguages: [python]\n"
    )
    # Add a key while preserving the existing ones.
    (repo / ".robotsix-mill" / "config.yaml").write_text(
        "test_command: pytest -q\n"
        "languages: [python]\n"
        "extra_sandbox_packages:\n  - pip:pytest\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: add extra_sandbox_packages")

    _no_triage(monkeypatch)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={".robotsix-mill/config.yaml"},
        settings=ctx.settings,
        spec="add extra_sandbox_packages",
        current_feedback=None,
    )

    # Guard did not interfere; the only changed file is in scope.
    assert result.action == "skip_iteration"
    assert result.outcome is None


def test_repo_settings_guard_blocks_dropped_keys(ctx_factory, tmp_path, monkeypatch):
    """(b) An edit that replaces the file with only extra_sandbox_packages,
    dropping test_command/smoke_command → guard blocks with feedback
    naming the dropped keys."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ".robotsix-mill/config.yaml")

    repo, branch = _seed_repo_settings(
        ctx,
        remote,
        t,
        "test_command: pytest -q\nsmoke_command: scripts/smoke.sh\n",
    )
    # Clobber the file — only the ticket-specific key remains.
    (repo / ".robotsix-mill" / "config.yaml").write_text(
        "extra_sandbox_packages:\n  - pip:pytest\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: clobber config")

    _no_triage(monkeypatch)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={".robotsix-mill/config.yaml"},
        settings=ctx.settings,
        spec="add extra_sandbox_packages",
        current_feedback=None,
    )

    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    note = result.outcome.note
    assert ".robotsix-mill/config.yaml" in note
    assert "test_command" in note
    assert "smoke_command" in note
    assert "do not rewrite the whole file" in note

    # A step event mirrors the block on the ticket timeline.
    events = [ev.note for ev in ctx.service.history(t.id) if ev.note]
    assert any("test_command" in n and "config.yaml" in n for n in events)


def test_repo_settings_guard_blocks_invalid_content(ctx_factory, tmp_path, monkeypatch):
    """(c) An edit introducing a wrong-typed key → guard blocks with the
    validator's problem in the feedback."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ".robotsix-mill/config.yaml")

    repo, branch = _seed_repo_settings(ctx, remote, t, "test_command: pytest -q\n")
    # Introduce a wrong-typed key (test_command must be a string).
    (repo / ".robotsix-mill" / "config.yaml").write_text("test_command: 5\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: wrong-typed key")

    _no_triage(monkeypatch)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={".robotsix-mill/config.yaml"},
        settings=ctx.settings,
        spec="edit config",
        current_feedback=None,
    )

    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "test_command" in result.outcome.note
    assert "config.yaml" in result.outcome.note


def test_repo_settings_guard_ignores_untouched_config(
    ctx_factory, tmp_path, monkeypatch
):
    """A ticket that does not touch .robotsix-mill/config.yaml is not
    affected by the guard (it returns None immediately)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    (repo / "a.txt").write_text("in scope")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: in scope only")

    _no_triage(monkeypatch)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=ctx.settings,
        spec="add a.txt",
        current_feedback=None,
    )

    # No config.yaml in the diff → guard is a no-op; in-scope change passes.
    assert result.action == "skip_iteration"
    assert result.outcome is None


# --- binary artifact auto-cleanup in scope guardrail ----------------------


def test_binary_artifact_auto_cleanup_skips_triage(ctx_factory, tmp_path, monkeypatch):
    """When all out-of-scope files are binary artifacts, the scope-triage
    LLM is NOT invoked, the binary files are auto-cleaned, and the result
    is skip_iteration (ticket continues to test gate)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create a binary artifact file (test.db) that is out-of-scope.
    db_path = repo / "test.db"
    db_path.write_bytes(b"\x00\x01\x02\x03SQLite format 3\0")
    _git(repo, "add", "test.db")
    _git(repo, "commit", "-q", "-m", "wip with binary")

    settings = ctx.settings

    # Mock scope-triage to verify it is NOT called.
    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called for binary artifacts")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    # a) scope-triage LLM NOT invoked
    assert len(triage_called) == 0, (
        "scope-triage agent should not be called for binary-only out-of-scope"
    )

    # b) binary file no longer exists on disk
    assert not db_path.exists(), "binary artifact should be removed from disk"

    # c) result is skip_iteration
    assert result.action == "skip_iteration"
    assert result.outcome is None

    # d) step event contains auto-REJECT with filename
    history = ctx.service.history(t.id)
    events = [ev.note for ev in history if ev.note]
    assert any(
        "scope-triage auto-REJECT (binary artifacts)" in note and "`test.db`" in note
        for note in events
    ), f"auto-REJECT step event missing; history events: {events}"


def test_binary_artifact_cleanup_with_text_files_still_calls_triage(
    ctx_factory, tmp_path, monkeypatch
):
    """When out-of-scope files include both a binary artifact AND a text
    file, the binary is auto-cleaned AND the text file is still passed to
    the scope-triage LLM (called exactly once, with only the text file)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create a binary artifact AND a text file, both out-of-scope.
    db_path = repo / "test.db"
    db_path.write_bytes(b"\x00\x01\x02\x03SQLite format 3\0")
    (repo / "README.md").write_text("out of scope text edit")
    _git(repo, "add", "test.db", "README.md")
    _git(repo, "commit", "-q", "-m", "wip with binary and text")

    settings = ctx.settings

    # Mock scope-triage to capture what files it receives.
    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    triage_calls = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_calls.append((out_of_scope_files, diff_summaries))
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="README.md is a natural side-effect edit",
            expand_files=["README.md"],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    # Binary file is removed from disk.
    assert not db_path.exists(), "binary artifact should be removed from disk"

    # Triage agent is called exactly once.
    assert len(triage_calls) == 1, (
        "scope-triage should be called exactly once for mixed out-of-scope"
    )

    out_of_scope_files, diff_summaries = triage_calls[0]

    # Only the text file is passed to triage.
    assert out_of_scope_files == ["README.md"], (
        f"expected only README.md, got {out_of_scope_files}"
    )
    assert "README.md" in diff_summaries
    assert "test.db" not in diff_summaries

    # Auto-REJECT step event was emitted for the binary.
    history = ctx.service.history(t.id)
    events = [ev.note for ev in history if ev.note]
    assert any(
        "scope-triage auto-REJECT (binary artifacts)" in note and "`test.db`" in note
        for note in events
    )

    # Result should be EXPAND (continue or skip_iteration depending on
    # whether expand files need re-run). Since README.md may or may not
    # already be in changed, either is fine — but it should not be a
    # return (which would mean BLOCKED).
    assert result.action in ("continue", "skip_iteration")


def test_binary_artifact_git_numstat_fallback(ctx_factory, tmp_path, monkeypatch):
    """A file without a known binary extension but detected by git numstat
    as binary is still auto-cleaned."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create a file with a non-binary extension but binary content
    # (git will treat it as binary).
    weird_path = repo / "datafile.dat"
    weird_path.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08")
    _git(repo, "add", "datafile.dat")
    _git(repo, "commit", "-q", "-m", "wip with misnamed binary")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 0
    assert not weird_path.exists(), "misnamed binary should be removed from disk"
    assert result.action == "skip_iteration"


def test_binary_artifact_untracked_file_cleanup(ctx_factory, tmp_path, monkeypatch):
    """An untracked binary file (created by agent runtime, never committed)
    is still detected and cleaned by os.unlink after git checkout is a no-op."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create an untracked binary file — NOT committed.
    untracked_db = repo / "mail.db"
    untracked_db.write_bytes(b"\x00\x01\x02\x03SQLite format 3\0")

    # Also modify a tracked, in-scope file so that changed_files returns
    # something we can work with alongside the untracked binary.
    (repo / "a.txt").write_text("modified in-scope file")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "in-scope change")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 0
    assert not untracked_db.exists(), (
        "untracked binary artifact should be removed from disk"
    )
    assert result.action == "skip_iteration"


# --- scope-triage flood guard ---------------------------------------------


def test_scope_triage_flood_guard_blocks(ctx_factory, tmp_path, monkeypatch):
    """When the out-of-scope TEXT file count exceeds
    scope_triage_max_files, the flood guard short-circuits: the
    scope-triage LLM is NEVER called, the result is BLOCKED, and a
    flood-guard step event with a truncated sample is recorded."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        scope_triage_max_files=5,
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create more out-of-scope text files than the cap (5) and well
    # past _FLOOD_SAMPLE_SIZE-independent truncation logic.
    n_files = 12
    for i in range(n_files):
        (repo / f"flood_{i:02d}.txt").write_text(f"flood file {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: artifact flood")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called for a flood")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 0, "LLM must not be called for a flood"
    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "flood guard" in result.outcome.note
    # 12 files > _FLOOD_SAMPLE_SIZE? No (20). But the message still
    # reports the count and cap.
    assert "12" in result.outcome.note
    assert "5" in result.outcome.note

    # A flood-guard step event was recorded.
    events = [ev.note for ev in ctx.service.history(t.id) if ev.note]
    assert any("scope-triage flood guard" in note for note in events)


def test_scope_triage_flood_guard_truncates_sample(ctx_factory, tmp_path, monkeypatch):
    """When the out-of-scope count exceeds _FLOOD_SAMPLE_SIZE, the
    operator-facing message truncates the sample with a '+N more' marker."""
    from robotsix_mill.stages.implement import _FLOOD_SAMPLE_SIZE

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        scope_triage_max_files=5,
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    n_files = _FLOOD_SAMPLE_SIZE + 5
    for i in range(n_files):
        (repo / f"flood_{i:03d}.txt").write_text(f"flood file {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: big artifact flood")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    monkeypatch.setattr(
        scope_triage_mod,
        "run_scope_triage_agent",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "flood guard" in result.outcome.note
    assert "more)" in result.outcome.note
    assert "+5 more" in result.outcome.note


def test_scope_triage_flood_guard_below_cap_calls_llm(
    ctx_factory, tmp_path, monkeypatch
):
    """Out-of-scope count <= cap: the guard does NOT trip — the
    scope-triage LLM is still invoked normally."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        scope_triage_max_files=5,
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Only 2 out-of-scope text files — well under the cap of 5.
    for i in range(2):
        (repo / f"small_{i}.txt").write_text(f"small file {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: small out-of-scope set")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(out_of_scope_files)
        return ScopeTriageVerdict(
            action="ESCALATE",
            justification="ambiguous",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 1, "LLM must be called below the cap"
    # ESCALATE → BLOCKED via the normal scope-triage path, NOT the flood guard.
    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "flood guard" not in (result.outcome.note or "")


# --- modules.yaml auto-EXPAND in scope guardrail --------------------------


def test_modules_yaml_repath_in_scope_auto_expands(ctx_factory, tmp_path, monkeypatch):
    """AC1: a refactor that re-paths in-scope modules in docs/modules.yaml
    is auto-EXPANDed — no LLM invoked, file_map gains the file."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
    )
    t = _ticket(ctx)

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Seed the base with old.py TRACKED and docs/modules.yaml pointing to it.
    (repo / "src" / "robotsix_mill").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "robotsix_mill" / "old.py").write_text("# old module")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n  - id: my_module\n    paths:\n      - src/robotsix_mill/old.py\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed old module")
    _git(repo, "push", "origin", "main")

    # Create the mill branch and move the module with git mv.
    branch = f"mill/{t.id}"
    _git(repo, "checkout", "-q", "-b", branch)
    pkg = repo / "src" / "robotsix_mill" / "pkg"
    pkg.mkdir(parents=True)
    _git(repo, "mv", "src/robotsix_mill/old.py", "src/robotsix_mill/pkg/new.py")

    # Re-path docs/modules.yaml to the new location.
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: my_module\n"
        "    paths:\n"
        "      - src/robotsix_mill/pkg/new.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "wip: move module")

    # file_map contains only the new path (the moved file).
    _write_file_map(ctx, t, "src/robotsix_mill/pkg/new.py")

    # Mock scope-triage to prove it is NOT called for this file.
    import robotsix_mill.agents.scope_triage as scope_triage_mod

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        raise AssertionError(
            "LLM must NOT be called — docs/modules.yaml should be "
            "auto-EXPANDed deterministically"
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={"src/robotsix_mill/pkg/new.py"},
        settings=ctx.settings,
        spec="move old.py to pkg/new.py",
        current_feedback=None,
    )

    # AC1: auto-EXPAND → skip_iteration
    assert result.action == "skip_iteration"
    assert result.file_map is not None
    assert "docs/modules.yaml" in result.file_map

    # Step event recording the auto-EXPAND was emitted.
    history = ctx.service.history(t.id)
    events = [ev.note for ev in history if ev.note]
    assert any(
        "scope-triage auto-EXPAND" in note
        and "docs/modules.yaml" in note
        and "registry sync" in note
        for note in events
    ), f"auto-EXPAND step event missing; history events: {events}"


def test_modules_yaml_new_unrelated_module_still_flagged(
    ctx_factory, tmp_path, monkeypatch
):
    """AC2: registering a NEW module in docs/modules.yaml with paths NOT in
    file_map is NOT auto-EXPANDed — it stays in out_of_scope and reaches
    the LLM (or blocks when triage is disabled)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
    )
    t = _ticket(ctx)

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Seed docs/modules.yaml with a paths: entry.
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n  - id: my_module\n    paths:\n      - src/robotsix_mill/legit.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "seed modules.yaml")
    _git(repo, "push", "origin", "main")

    # Create mill branch with a legitimate in-scope change AND an
    # unrelated modules.yaml addition.
    branch = f"mill/{t.id}"
    _git(repo, "checkout", "-q", "-b", branch)

    # In-scope change: the legit file.
    (repo / "src" / "robotsix_mill").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "robotsix_mill" / "legit.py").write_text("# legit change")
    _git(repo, "add", "src/robotsix_mill/legit.py")

    # Unrelated: register a brand-new module entry in modules.yaml.
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: my_module\n"
        "    paths:\n"
        "      - src/robotsix_mill/legit.py\n"
        "  - id: unrelated_module\n"
        "    paths:\n"
        "      - src/robotsix_mill/unrelated.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "wip: legit + unrelated registry")

    # file_map contains only the legitimate file.
    _write_file_map(ctx, t, "src/robotsix_mill/legit.py")

    # Mock scope-triage to capture what out_of_scope_files it receives.
    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    triage_calls = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_calls.append((out_of_scope_files, diff_summaries))
        return ScopeTriageVerdict(
            action="REJECT",
            justification="Unrelated module registered",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={"src/robotsix_mill/legit.py"},
        settings=ctx.settings,
        spec="update legit.py",
        current_feedback=None,
    )

    # The triage agent WAS called (docs/modules.yaml was NOT auto-EXPANDed).
    assert len(triage_calls) == 1, (
        "scope-triage should be called because unrelated module path is not in file_map"
    )
    out_of_scope_files, _ = triage_calls[0]
    assert "docs/modules.yaml" in out_of_scope_files, (
        "docs/modules.yaml should remain in out_of_scope_files"
    )
    # The guardrail returns because the LLM issued REJECT.
    assert result.action == "return"


def test_modules_yaml_added_paths_parses_diff(tmp_path):
    """Unit test: _modules_yaml_added_paths correctly extracts added path
    tokens from a git diff, ignoring removed lines, comments, and non-path
    YAML keys."""
    from robotsix_mill.stages.implement import _modules_yaml_added_paths

    # Build a minimal git repo with a base and a branch that modifies
    # docs/modules.yaml.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")

    # Base commit: empty modules.yaml.
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: existing\n"
        "    description: already present\n"
        "    paths:\n"
        "      - src/robotsix_mill/existing.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "base")
    # Create a fake remote ref so origin/main resolves.
    _git(repo, "branch", "-M", "main")

    # Modify: add new paths, a comment line, a description line, and
    # delete an old path. The helper must:
    # - pick up the added paths (renamed.py, brand_new.py)
    # - ignore the removed path (existing.py)
    # - ignore comment/description/id lines.
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: existing\n"
        "    description: already present (updated description)\n"
        "    paths:\n"
        "      - src/robotsix_mill/renamed.py\n"
        "      - src/robotsix_mill/brand_new.py\n"
        "  # comment line\n"
    )
    _git(repo, "add", "docs/modules.yaml")

    # We need origin/main to be the base commit. Since we can't easily
    # make a real remote, we use a trick: tag the base commit as a
    # substitute for origin/main in the git diff call. Actually, the
    # helper uses `origin/{target_branch}`, so we need a real remote.
    # Create a bare clone as the "remote":
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(repo), str(bare)],
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", f"file://{bare}")
    # Fetch so origin/main is known locally.
    _git(repo, "fetch", "-q", "origin")

    # Now the helper should diff HEAD (uncommitted) against origin/main.
    added = _modules_yaml_added_paths(repo, "main")

    assert "src/robotsix_mill/renamed.py" in added, (
        f"expected renamed.py in added paths, got {added}"
    )
    assert "src/robotsix_mill/brand_new.py" in added, (
        f"expected brand_new.py in added paths, got {added}"
    )
    # Removed path must NOT appear.
    assert "src/robotsix_mill/existing.py" not in added, (
        "removed path existing.py should not be in added paths"
    )
    # Non-path lines must NOT appear.
    for non_path in (
        "description:",
        "id:",
        "modules:",
        "# comment line",
    ):
        assert non_path not in added, (
            f"non-path token {non_path!r} should not be in added paths"
        )
    # Comment and description variants.
    assert "already present (updated description)" not in added


# --- test-baseline check -------------------------------------------------


def test_baseline_check_blocks_on_failure(ctx_factory, tmp_path, monkeypatch):
    """AC1: pre-existing base-branch test failures block before the loop."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

    def _fake_agent_run(*a, **kw):
        agent_called.append(1)
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    # Force the baseline check to fail.
    def _failing_test_agent(
        *, settings, repo_dir, repo_config=None, retry_on_failure=False
    ):
        return False, "tests failed (rc=1); pre-existing failure"

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent", _failing_test_agent
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "pre-existing test failures" in out.note
    assert "main" in out.note  # forge_target_branch
    # The agent loop must never be entered.
    assert len(agent_called) == 0

    # implement.md artifact must exist.
    artifacts = ctx.service.workspace(t).artifacts_dir
    assert (artifacts / "implement.md").exists()
    content = (artifacts / "implement.md").read_text(encoding="utf-8")
    assert "BLOCKED" in content


def test_baseline_check_skipped_for_baseline_fix_ticket(
    ctx_factory, tmp_path, monkeypatch
):
    """Regression: a baseline-fix ticket (source=IMPLEMENT_BASELINE_DEPENDENCY)
    must NOT re-run the baseline gate.

    Such a ticket exists to repair the red base, so it has to implement
    AGAINST that still-red base. Re-running the gate on it would spawn yet
    another baseline fix, which dedups to the ticket itself
    ("Ticket cannot depend on itself" -> Fatal), deadlocking the ticket and
    every ticket parked behind it (board-wide deadlock).
    """
    from robotsix_mill.core.models import SourceKind

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    # Spy: the baseline gate must never be entered for this source. If the
    # guard regresses, this records a call and the assertion below fails.
    baseline_calls: list[int] = []
    monkeypatch.setattr(
        ImplementStage,
        "_run_baseline_check",
        staticmethod(lambda *a, **kw: baseline_calls.append(1)),
    )

    agent_called: list[int] = []

    def _fake_agent_run(*, settings, repo_dir, **_kwargs):
        del settings
        agent_called.append(1)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    t = ctx.service.create(
        "baseline: pre-existing test failures — main abc1234",
        "Repair the red base.",
        source=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
    )
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    # The baseline gate was skipped for the baseline-fix ticket ...
    assert baseline_calls == []
    # ... and the implement loop ran normally against the (red) base.
    assert len(agent_called) == 1
    assert out.next_state is State.DOCUMENTING


def test_baseline_checks_out_remote_base_sha_not_local_branch(
    ctx_factory, tmp_path, monkeypatch
):
    """Regression: the baseline must check out the EXACT origin/<branch>
    commit (base_sha), not the clone's possibly-stale local branch ref.

    The old `checkout(repo, "main")` ran whatever the local main pointed at
    — often stale — while labelling the result with the fresh remote SHA, so
    a fix that already landed on main was reported as still-failing and
    poisoned the gate. Assert the baseline checks out a 40-hex SHA.
    """
    from robotsix_mill.vcs import git_ops

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    calls: list[str] = []
    real_checkout = git_ops.checkout

    def _spy(repo, name):
        calls.append(name)
        real_checkout(repo, name)

    monkeypatch.setattr(git_ops, "checkout", _spy)
    # Fail the baseline so the run stops right after it (no real branch ops).
    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent",
        lambda **kw: (False, "pre-existing"),
    )
    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        lambda *a, **kw: ("done", [], "", None, None, False, ""),
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")
    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    # A full SHA was checked out for the baseline — not the bare branch name.
    assert any(
        len(c) == 40 and all(ch in "0123456789abcdef" for ch in c) for c in calls
    ), calls


def test_baseline_check_proceeds_on_pass(ctx_factory, tmp_path, monkeypatch):
    """AC2: passing baseline → loop proceeds normally."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

    def _fake_agent_run(
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
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    # Baseline check passes via fake_sandbox (test_command="true" → rc=0).
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1


def test_baseline_check_no_test_command(ctx_factory, tmp_path, monkeypatch):
    """AC3: no test_command → baseline passes trivially → loop proceeds."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="",  # empty → run_test_agent returns (True, ...)
        review_enabled="false",
    )

    agent_called = []

    def _fake_agent_run(
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
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1

    # Cache must exist with passed=true.
    cache_path = ctx.service.workspace(t).artifacts_dir / "baseline_check.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["passed"] is True
    assert "no test gate configured" in cache["diagnosis"]


def test_baseline_check_cached_on_retry(ctx_factory, tmp_path, monkeypatch):
    """AC4: cached baseline failure is reused on retry — no re-execution."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    call_count = [0]

    def _counted_test_agent(
        *, settings, repo_dir, repo_config=None, retry_on_failure=False
    ):
        call_count[0] += 1
        return False, "pre-existing failure"

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent", _counted_test_agent
    )
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent(None))

    t = _ticket(ctx)

    # First run: baseline check runs, blocks.
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.BLOCKED
    assert call_count[0] == 1

    # Second run (resume): cache hit, no re-execution.
    # The ticket is still BLOCKED; we simulate a resume by re-running
    # (the stage calls _clone_and_branch which will do a fresh clone,
    # but the cache is still on disk from the first run).
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert call_count[0] == 1  # still 1 — no second invocation


def test_baseline_check_sha_invalidation(ctx_factory, tmp_path, monkeypatch):
    """AC5: cached failure with old SHA → re-runs when base advances."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    call_count = [0]
    # First call fails, second call passes (simulating operator fix).
    results = [(False, "old failure"), (True, "all passed")]

    def _counted_test_agent(
        *, settings, repo_dir, repo_config=None, retry_on_failure=False
    ):
        idx = min(call_count[0], len(results) - 1)
        passed, diag = results[idx]
        call_count[0] += 1
        return passed, diag

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent", _counted_test_agent
    )

    t = _ticket(ctx)

    # First run: baseline check fails, caches result.
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.BLOCKED
    assert call_count[0] == 1

    # Tamper with the cache: change the base_sha so it no longer
    # matches the current remote SHA.  This simulates the base
    # branch advancing.
    cache_path = ctx.service.workspace(t).artifacts_dir / "baseline_check.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["base_sha"] = "0000000000000000000000000000000000000000"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    # Now re-run: cache SHA mismatch → re-execute.
    # Also need to bypass the agent since this time the test passes.
    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out2 = ImplementStage().run(t, ctx)
    # The second call to _counted_test_agent returned (True, ...) → proceed.
    assert out2.next_state is State.DOCUMENTING
    # Baseline re-executed (call 2); per-iteration test gate may add more.
    assert call_count[0] >= 2  # re-executed

    # Cache updated with new result.
    cache2 = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache2["passed"] is True


def test_baseline_check_sandbox_unavailable(ctx_factory, tmp_path, monkeypatch):
    """AC6: sandbox unavailable during baseline check → transient SandboxError.

    A sandbox that fails to launch is infrastructure, not a pre-existing
    failure on the base: it must raise a transient error (worker retries
    with backoff) rather than caching a bogus baseline failure, blocking the
    ticket, and spawning a phantom baseline-fix.
    """
    from robotsix_mill.runtime.transient_errors import classify_stage_error
    from robotsix_mill.sandbox import SandboxError

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    def _sandbox_error(*, settings, repo_dir, repo_config=None, retry_on_failure=False):
        return False, "sandbox unavailable: Docker daemon not running"

    monkeypatch.setattr("robotsix_mill.stages.implement.run_test_agent", _sandbox_error)

    t = _ticket(ctx)

    with pytest.raises(SandboxError) as excinfo:
        ImplementStage().run(t, ctx)
    assert "sandbox unavailable" in str(excinfo.value)
    assert classify_stage_error(excinfo.value) == "transient"

    # No bogus "passed: False" baseline cache — a retry re-attempts cleanly.
    cache_path = ctx.service.workspace(t).artifacts_dir / "baseline_check.json"
    assert not cache_path.exists()


def test_baseline_gate_proceeds_when_dependency_fix_done(
    ctx_factory, tmp_path, monkeypatch
):
    """Idempotency: a ticket whose baseline-fix dependency has reached DONE
    for THIS base_sha must NOT re-spawn a duplicate fix — it proceeds.

    Without the guard, on re-entry origin/main is unchanged (the fix lives on
    its own unmerged branch) → same base_sha → cached/fresh FAILING result →
    a brand-new baseline-fix is spawned (the prior DONE one is invisible to
    the open-only dedup), wedging the ticket in an operator-only re-spawn
    cycle.
    """
    from robotsix_mill.core.models import SourceKind

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    # Force a deterministic resolved base SHA so the title is predictable.
    base_sha = "a1b2c3d4" + "0" * 32
    monkeypatch.setattr(git_ops, "remote_branch_sha", lambda *a, **kw: base_sha)
    # The baseline test agent reports FAILING — without the guard this would
    # re-spawn a fix.
    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent",
        lambda **kw: (False, "pre-existing failure"),
    )

    fix_title = ImplementStage._baseline_fix_title(
        ctx.settings, base_sha, ctx.settings.forge_target_branch
    )
    fix = ctx.service.create(
        fix_title,
        "Repair the red base.",
        source=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
    )
    ctx.service.transition(fix.id, State.DONE)

    t = ctx.service.create("Add feature", "Please add feature.txt")
    ctx.service.set_depends_on(t.id, [fix.id])
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)

    before = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )

    out = ImplementStage._run_baseline_check(
        ctx, t, tmp_path, f"mill/{t.id}", False, ctx.settings
    )

    # Proceeds (no short-circuit Outcome) ...
    assert out is None
    # ... and no NEW baseline-fix ticket was spawned.
    after = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )
    assert after == before


def test_baseline_gate_spawns_when_dependency_fix_for_different_sha(
    ctx_factory, tmp_path, monkeypatch
):
    """The guard must NOT fire when the depended-on baseline-fix is for a
    DIFFERENT base_sha (different title) — normal gating still spawns/parks.
    """
    from robotsix_mill.core.models import SourceKind

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent",
        lambda **kw: (False, "pre-existing failure"),
    )
    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        lambda *a, **kw: ("done", [], "", None, None, False, ""),
    )

    # A DONE baseline-fix for an unrelated base_sha — its title differs from
    # the title computed for the real base, so the guard does not fire.
    other_sha = "deadbeef" + "0" * 32
    fix = ctx.service.create(
        ImplementStage._baseline_fix_title(
            ctx.settings, other_sha, ctx.settings.forge_target_branch
        ),
        "Repair some other red base.",
        source=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
    )
    ctx.service.transition(fix.id, State.DONE)

    t = ctx.service.create("Add feature", "Please add feature.txt")
    ctx.service.set_depends_on(t.id, [fix.id])
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    before = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )

    out = ImplementStage().run(t, ctx)

    # Normal gating: pre-existing failure parks the ticket BLOCKED ...
    assert out.next_state is State.BLOCKED
    assert "pre-existing test failures" in out.note
    # ... and a NEW baseline-fix was spawned (guard did not suppress it).
    after = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )
    assert after == before + 1


# --- prerequisite gate --------------------------------------------------


def _no_prereq_block_spec():
    return "## Problem\nDo a thing.\n## Acceptance criteria\n- works\n"


def test_prereq_gate_disabled_never_checks(ctx_factory, tmp_path, monkeypatch):
    """Gate explicitly disabled: run_prerequisite_check is never called and
    behaviour is unchanged — the stage proceeds to the agent."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="false",
    )
    assert ctx.settings.prerequisite_gate_enabled is False

    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return {"unmet": [], "reason": "x"}

    monkeypatch.setattr(prerequisite, "run_prerequisite_check", _spy)
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert called["n"] == 0


def test_prereq_gate_unmet_blocks_without_agent(ctx_factory, tmp_path, monkeypatch):
    """Gate enabled + an unmet prerequisite → BLOCKED, naming the
    directive, WITHOUT invoking run_implement_agent."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )

    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {
            "unmet": ["symbol CostLogSource from robotsix_llmio"],
            "reason": "unmet",
        },
    )

    def _boom(*a, **kw):
        raise AssertionError("run_implement_agent must NOT be called")

    monkeypatch.setattr(coding, "run_implement_agent", _boom)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "CostLogSource" in out.note
    assert "prerequisite" in out.note.lower()


def test_prereq_gate_default_activation_blocks_without_agent(
    ctx_factory, tmp_path, monkeypatch
):
    """Flag left at its NEW default (not set explicitly) + an unmet
    prerequisite → BLOCKED, WITHOUT invoking run_implement_agent. Proves
    the default activation works end-to-end."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    # The flag is on by default now — the test must NOT set it.
    assert ctx.settings.prerequisite_gate_enabled is True

    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {
            "unmet": ["symbol CostLogSource from robotsix_llmio"],
            "reason": "unmet",
        },
    )

    def _boom(*a, **kw):
        raise AssertionError("run_implement_agent must NOT be called")

    monkeypatch.setattr(coding, "run_implement_agent", _boom)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "CostLogSource" in out.note
    assert "prerequisite" in out.note.lower()


def test_prereq_gate_met_proceeds(ctx_factory, tmp_path, monkeypatch):
    """Gate enabled + all prerequisites met → stage proceeds to the
    agent exactly as before."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )
    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {"unmet": [], "reason": "ok"},
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


def test_prereq_gate_best_effort_on_error(ctx_factory, tmp_path, monkeypatch):
    """Gate enabled but run_prerequisite_check raises → stage logs a
    warning and proceeds (best-effort), rather than blocking."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )

    def _boom(*a, **kw):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(prerequisite, "run_prerequisite_check", _boom)
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


# ---------------------------------------------------------------------------
# prepare hook integration tests
# ---------------------------------------------------------------------------


def test_prepare_hook_failure_blocks_before_prerequisite_gate(
    ctx_factory,
    tmp_path,
    monkeypatch,
):
    """When ``run_prepare_hook`` returns an error, implement short-circuits
    to BLOCKED with that error BEFORE the prerequisite gate runs."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )

    prereq_called = []

    def _spy_prereq(*args, **kwargs):
        prereq_called.append(1)

    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        _spy_prereq,
    )
    monkeypatch.setattr(
        ImplementStage,
        "_run_prerequisite_gate",
        _spy_prereq,
    )

    from robotsix_mill.stages import hooks as hooks_mod

    monkeypatch.setattr(
        hooks_mod,
        "run_prepare_hook",
        lambda repo_dir, ticket_id, workspace_dir: (
            "prepare hook exited 2: setup failed"
        ),
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "dummy.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "prepare hook exited 2" in out.note
    assert "setup failed" in out.note
    # Prerequisite gate must NOT have been called — the hook blocked first.
    assert len(prereq_called) == 0


# ── gitignored-edit detection (manifest boards: writes git can't see) ──


def test_claimed_gitignored_edits_detects_invisible_writes(tmp_path):
    """Edit tool-calls that landed in a gitignored sub-tree (the
    robotsix-mill-ros2 ``/src/*`` manifest layout) are named, so the
    'no changes produced' block tells the operator WHAT happened."""
    import json as _json

    remote = make_bare_repo(tmp_path)
    repo_dir = tmp_path / "clone"
    git_ops.clone(remote, repo_dir, "main")
    (repo_dir / ".gitignore").write_text("/src/*\n")
    git_ops.commit_all(repo_dir, "ignore vendored sources")
    target = repo_dir / "src" / "pkg" / "msg"
    target.mkdir(parents=True)
    (target / "Status.msg").write_text("int32 code\n")
    (repo_dir / "tracked.txt").write_text("visible\n")

    msgs = _json.dumps(
        [
            {
                "parts": [
                    {
                        "part_kind": "tool-call",
                        "tool_name": "write_file",
                        "args": {"path": "src/pkg/msg/Status.msg"},
                        "tool_call_id": "c1",
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_name": "write_file",
                        "args": {"path": "tracked.txt"},
                        "tool_call_id": "c2",
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_name": "Write",
                        # absolute path INSIDE the clone (Claude SDK style)
                        "args": {"file_path": str(target / "Status.msg")},
                        "tool_call_id": "c3",
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_name": "Write",
                        # absolute path OUTSIDE the clone → skipped
                        "args": {"file_path": "/etc/hosts"},
                        "tool_call_id": "c4",
                    },
                ]
            }
        ]
    ).encode()

    hits = ImplementStage._claimed_gitignored_edits(repo_dir, msgs)
    assert hits == ["src/pkg/msg/Status.msg"]


def test_claimed_gitignored_edits_fail_open(tmp_path):
    """Malformed input never raises — the detector only enriches notes."""
    assert ImplementStage._claimed_gitignored_edits(tmp_path, b"{bad") == []
    assert ImplementStage._claimed_gitignored_edits(tmp_path, None) == []


def test_scope_triage_new_file_summary_shows_content(
    ctx_factory, tmp_path, monkeypatch
):
    """NEW (untracked) out-of-scope files have an empty ``git diff`` vs the
    base; the triage agent then sees no content and ESCALATEs blindly (live
    case: the worker.py package refactor cb63 — every new submodule
    summarized empty). The summary must fall back to the file head."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
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

    def _run(*, settings, repo_dir, spec, **_kwargs):
        del settings, spec
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        # Brand-new file, never tracked → empty `git diff origin/main -- f`.
        (Path(repo_dir) / "brand_new_module.py").write_text(
            "def shiny_new_helper():\n    return 42\n"
        )
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    captured: dict = {}

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        captured["summaries"] = dict(diff_summaries)
        return ScopeTriageVerdict(
            action="ESCALATE",
            justification="capture only",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    ImplementStage().run(t, ctx)

    summary = captured["summaries"]["brand_new_module.py"]
    assert "NEW FILE" in summary
    assert "shiny_new_helper" in summary


# ------------------------------------------------------------------


def test_convergence_backstop_halts_at_cycle_cap(ctx_factory, tmp_path, monkeypatch):
    """The preflight gate escalates to BLOCKED when
    ``implement_cycles`` reaches ``max_implement_review_cycles``.
    """
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        max_implement_review_cycles="2",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    # Seed the counter at the cap so preflight trips it.
    ctx.service.set_implement_cycles(t.id, 2)
    # Reload so ticket.implement_cycles reflects the set value.
    t = ctx.service.get(t.id)
    assert t.implement_cycles == 2

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "cycle limit reached" in out.note.lower()
    assert "2/2" in out.note


def test_convergence_empty_diff_after_review_terminates_done(
    ctx_factory, tmp_path, monkeypatch
):
    """When a ticket returns from review (review_rounds > 0) and the
    branch has no commits beyond origin/main, there is genuinely nothing
    to merge — implement terminates DONE (already satisfied) instead of
    looping in BLOCKED (ticket 0976).
    """
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="true",
        max_implement_review_cycles="10",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    # Bypass gates that require a real sandbox / API key.
    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Run implement once so the branch exists (creating the clone).
    # The agent produces a simple change that gets committed.
    def _run_once(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_once)
    out1 = ImplementStage().run(t, ctx)
    # With review_enabled=True, the first pass should proceed to CODE_REVIEW.
    assert out1.next_state is State.CODE_REVIEW

    # Now simulate returning from review: set review_rounds > 0 and
    # RESET the branch so it has no commits beyond origin/main.
    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    branch = f"{ctx.settings.branch_prefix}{t.id}"
    target = "main"
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", f"origin/{target}"],
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

    # Second implement run: resuming=True, review_rounds>0, branch has no
    # commits ahead → genuine no-op → terminate DONE (already satisfied).
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.DONE
    assert "already satisfied" in out2.note.lower()
    assert "empty diff" in out2.note.lower()
