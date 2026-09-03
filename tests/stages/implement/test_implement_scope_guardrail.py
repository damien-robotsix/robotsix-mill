import subprocess

from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import ImplementStage
from robotsix_mill.vcs import git_ops
from tests.stages.implement.conftest import _ticket, _write_file_map
from tests.stages.implement.test_implement import (
    _clone_repo_to,
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
