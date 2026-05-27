import json
import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.agents.fs_tools import build_fs_tools
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.implement import ImplementStage
from robotsix_mill.vcs import git_ops


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
        s = Settings(MILL_DATA_DIR=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s)
        svc = TicketService(s)
        created.append(s)
        from robotsix_mill.config import RepoConfig; return StageContext(settings=s, service=svc, repo_config=RepoConfig(repo_id="test-repo", board_id="test-board", langfuse_project_name="test", langfuse_public_key="pk-test", langfuse_secret_key="sk-test"))

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
    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path, previous_attempt_summary  # signature must match the seam
        if write:
            for name, content in write.items():
                (Path(repo_dir) / name).write_text(content)
        return ("did the thing", list(write.keys()) if write else [], "")

    return _run


# --- fs_tools sandbox ---------------------------------------------------

def test_fs_tools_roundtrip_and_sandbox(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    read_file, write_file, edit_file, delete_file, list_dir, run_command = build_fs_tools(
        tmp_path, s
    )
    assert "wrote" in write_file("a/b.txt", "hi")
    assert read_file(path="a/b.txt") == "hi"
    assert "a/" in list_dir(".")
    assert "exit=0" in run_command("echo ok")
    # errors come back as strings (so the model can self-correct), and
    # the path-escape guard still refuses the op
    esc = read_file(path="../escape.txt")
    assert esc.startswith("error:") and "escapes" in esc
    assert read_file(path="nope.txt").startswith("error:")  # missing file


def test_write_file_unchanged(tmp_path, fake_sandbox):
    """Existing write_file roundtrip still works identically."""
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    read_file, write_file, *_ = build_fs_tools(tmp_path, s)
    assert "wrote" in write_file("x.txt", "hello world")
    assert read_file(path="x.txt") == "hello world"


def test_edit_file_replaces_unique_substring_preserves_rest(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
    original = "line1\nline2\nline3\nline4\n"
    (tmp_path / "f.txt").write_text(original)
    result = edit_file("f.txt", "line2", "REPLACED")
    assert "replaced 1 occurrence" in result
    new = (tmp_path / "f.txt").read_text()
    assert "REPLACED" in new
    assert "line2" not in new
    # surrounding lines byte-identical
    assert new == "line1\nREPLACED\nline3\nline4\n"


def test_edit_file_old_string_absent_returns_error_file_unchanged(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
    original = "line1\nline2\n"
    (tmp_path / "f.txt").write_text(original)
    result = edit_file("f.txt", "nonexistent", "X")
    assert "not found" in result
    assert (tmp_path / "f.txt").read_text() == original


def test_edit_file_old_string_appears_multiple_returns_error_file_unchanged(
    tmp_path, fake_sandbox,
):
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
    original = "dup\nmiddle\ndup\n"
    (tmp_path / "f.txt").write_text(original)
    result = edit_file("f.txt", "dup", "X")
    assert "appears 2 times" in result
    assert (tmp_path / "f.txt").read_text() == original


def test_edit_file_path_escape_rejected(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
    result = edit_file("../outside.txt", "x", "y")
    assert "escapes" in result


def test_delete_file_removes_existing_file(tmp_path, fake_sandbox):
    """delete_file returns success and the file no longer exists."""
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    (tmp_path / "foo.txt").write_text("hello")
    result = delete_file("foo.txt")
    assert "deleted" in result
    assert not (tmp_path / "foo.txt").exists()


def test_delete_file_missing_returns_error(tmp_path, fake_sandbox):
    """delete_file on a missing file returns an error string, not a crash."""
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    result = delete_file("nope.txt")
    assert result.startswith("error:")


def test_delete_file_on_directory_returns_error(tmp_path, fake_sandbox):
    """delete_file on a directory returns an error string, no deletion."""
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    d = tmp_path / "subdir"
    d.mkdir()
    result = delete_file("subdir")
    assert result.startswith("error:")
    assert d.exists()  # directory untouched


def test_delete_file_path_escape_rejected(tmp_path, fake_sandbox):
    """Path traversal is rejected by _safe."""
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    result = delete_file("../outside.txt")
    assert "escapes" in result


def test_fs_tools_non_existent_root_returns_clear_error(tmp_path, fake_sandbox):
    """Every tool returns a stable error string (not a raw exception)
    when the workspace repo directory hasn't been cloned yet."""
    from robotsix_mill.config import Settings

    fake_root = tmp_path / "does-not-exist"
    s = Settings(MILL_DATA_DIR=str(tmp_path))
    read_file, write_file, edit_file, delete_file, list_dir, run_command = build_fs_tools(
        fake_root, s
    )
    msg = "workspace repo directory does not exist"

    assert msg in read_file(path="anything.txt")
    assert msg in write_file("x.txt", "content")
    assert msg in edit_file("x.txt", "a", "b")
    assert msg in delete_file("x.txt")
    assert msg in list_dir(".")
    # run_command does NOT go through _safe — it calls sandbox.run()
    # directly. When the repo_dir doesn't exist, _repo_mount rejects it.
    assert "repo" in run_command("true").lower()


# --- implement stage ----------------------------------------------------

def test_blocked_without_remote(ctx_factory):
    ctx = ctx_factory(MILL_TEST_COMMAND="true")
    out = ImplementStage().run(_ticket(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "FORGE_REMOTE_URL" in out.note


def test_success_to_deliverable(ctx_factory, tmp_path, monkeypatch):
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    repo = ctx.service.workspace(t).dir / "repo"
    assert (repo / "feature.txt").exists()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == f"mill/{t.id}"
    assert ctx.service.get(t.id).branch == f"mill/{t.id}"
    assert (ctx.service.workspace(t).artifacts_dir / "implement.md").exists()


def test_no_changes_blocks(ctx_factory, tmp_path, monkeypatch):
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent(None))
    t = _ticket(ctx)
    _write_file_map(ctx, t, "dummy.txt")
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "no changes" in out.note


def test_failing_gate_blocks_resumable(ctx_factory, tmp_path, monkeypatch):
    """The stage owns a bounded fix loop: it re-invokes the coordinator
    on each test-gate failure, feeding the diagnosis back, and escalates
    to BLOCKED-resumable once max_fix_iterations is exhausted — WIP
    committed."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        MILL_TEST_COMMAND="false",        # gate always fails
        MILL_MAX_FIX_ITERATIONS="2",      # keep the loop short
    )
    calls = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, reference_files, message_history, memory, epic_workspace_path  # seam signature
        calls.append(feedback)
        (Path(repo_dir) / "wip.txt").write_text("did work")
        return ("tried", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)  # test_failing_gate_blocks_resumable

    assert out.next_state is State.BLOCKED
    assert "still failing" in out.note and "resumable" in out.note
    # The stage re-invokes the coordinator once per iteration.
    assert len(calls) == 2
    assert calls[0] is None              # first pass: no feedback
    assert calls[1] is not None          # retry: prior diagnosis fed back
    repo = ctx.service.workspace(t).dir / "repo"
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True, text=True,
    ).stdout
    assert "WIP" in log  # WIP committed so a human can pick it up


def _commits(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "log", "--pretty=%s"],
        capture_output=True, text=True,
    ).stdout.splitlines()


def test_budget_error_blocks_resumable_with_wip(ctx_factory, tmp_path, monkeypatch):
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        (Path(repo_dir) / "partial.txt").write_text("half done")
        raise coding.AgentBudgetError("request_limit of 50", [])

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "partial.txt")

    out = ImplementStage().run(t, ctx)  # test_budget_error_blocks_resumable_with_wip

    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note and "budget" in out.note
    # WIP committed so a human can resume (no transcript now — a resume
    # re-runs the coordinator fresh).
    ws = ctx.service.workspace(t)
    assert "WIP" in _commits(ws.dir / "repo")[0]
    # Artifacts written even on BLOCKED-as-resumable path.
    assert (ws.artifacts_dir / "reference_files.json").exists()
    assert (ws.artifacts_dir / "implement_summary.md").exists()


def test_resume_reruns_coordinator_without_reclone(ctx_factory, tmp_path, monkeypatch):
    """Resume = run the coordinator FRESH (no transcript replay), and
    crucially do NOT re-clone — the prior WIP branch is reused."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")
    n = {"i": 0}

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        n["i"] += 1
        if n["i"] == 1:  # first pass: partial work, hit the cap
            (Path(repo_dir) / "first.txt").write_text("1")
            raise coding.AgentBudgetError("cap", [])
        (Path(repo_dir) / "second.txt").write_text("2")
        return ("finished on resume", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "first.txt", "second.txt")

    first = ImplementStage().run(t, ctx)
    assert first.next_state is State.BLOCKED
    repo = ctx.service.workspace(t).dir / "repo"
    git_inode = (repo / ".git").stat().st_ino  # detect a re-clone

    # worker applies the Outcome; operator moves it back to READY
    ctx.service.transition(t.id, first.next_state, first.note)
    ctx.service.transition(t.id, State.READY, "retry")
    second = ImplementStage().run(ctx.service.get(t.id), ctx)

    assert second.next_state is State.DOCUMENTING
    assert n["i"] == 2                                      # coordinator re-run
    assert (repo / ".git").stat().st_ino == git_inode      # NOT re-cloned
    assert (repo / "first.txt").exists()                   # prior WIP kept
    assert (repo / "second.txt").exists()
    msgs = _commits(repo)
    assert any("WIP" in m for m in msgs) and len(msgs) >= 2


# --- unconditional rebase (fresh clone + resume) -----------------------


def _add_commit_to_bare_remote(bare_url: str, tmp_path: Path) -> str:
    """Add a commit to a bare remote (file:// URL) and return the file name.

    Clones the bare repo into a temp working dir, adds a file, commits,
    and pushes back to the bare remote. Returns the filename created.
    """
    import uuid
    wd = tmp_path / f"push-tmp-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["git", "clone", "-q", bare_url, str(wd)],
        check=True, capture_output=True, text=True,
    )
    _git(wd, "config", "user.email", "op@t")
    _git(wd, "config", "user.name", "operator")
    fname = "operator_edit.txt"
    (wd / fname).write_text("operator change on main\n")
    _git(wd, "add", "-A")
    _git(wd, "commit", "-q", "-m", "operator edit")
    _git(wd, "push", "origin", "main")
    return fname


def _conflicting_edit_on_remote(bare_url: str, tmp_path: Path) -> None:
    """Push a conflicting edit to README.md on the bare remote."""
    import uuid
    wd = tmp_path / f"conflict-tmp-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["git", "clone", "-q", bare_url, str(wd)],
        check=True, capture_output=True, text=True,
    )
    _git(wd, "config", "user.email", "op@t")
    _git(wd, "config", "user.name", "operator")
    (wd / "README.md").write_text("conflicting edit from remote\n")
    _git(wd, "add", "-A")
    _git(wd, "commit", "-q", "-m", "conflicting remote edit")
    _git(wd, "push", "origin", "main")


def test_fresh_clone_rebases_onto_new_remote_commit(ctx_factory, tmp_path, monkeypatch):
    """When a fresh clone materialises and origin/<target> has advanced
    since the clone (simulated by pushing *after* an initial clone that
    we discard), the rebase step picks up the new commit before the
    agent runs."""
    remote = make_bare_repo(tmp_path)

    # Push a second commit to the remote so it has README.md + operator_edit.txt.
    fname = _add_commit_to_bare_remote(remote, tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )

    seen_files: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        # Record what the agent can see in the working tree.
        for p in sorted(Path(repo_dir).iterdir()):
            if p.name != ".git":
                seen_files.append(p.name)
        (Path(repo_dir) / "agent_out.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "agent_out.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    # The agent must see the operator's edit that landed on the remote
    # before the clone — proving the rebase brought it in (even though
    # in this case the clone also got it; the rebase is a no-op when the
    # clone already has the latest).
    assert fname in seen_files


def test_resume_rebases_onto_new_remote_commit(ctx_factory, tmp_path, monkeypatch):
    """Resume path: after a budget-cap BLOCKED run, a new commit lands
    on origin/main.  On resume the rebase picks it up and the agent
    sees the new file."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )
    n = {"i": 0}

    seen_files: list[list[str]] = [[], []]

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        idx = n["i"]
        n["i"] += 1
        if idx == 0:
            (Path(repo_dir) / "first.txt").write_text("1")
            raise coding.AgentBudgetError("cap", [])
        # idx == 1: resume
        for p in sorted(Path(repo_dir).iterdir()):
            if p.name != ".git":
                seen_files[1].append(p.name)
        (Path(repo_dir) / "second.txt").write_text("2")
        return ("finished on resume", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "first.txt", "second.txt")

    first = ImplementStage().run(t, ctx)
    assert first.next_state is State.BLOCKED

    # Simulate an operator edit landing on the remote while the ticket
    # is BLOCKED.
    fname = _add_commit_to_bare_remote(remote, tmp_path)

    ctx.service.transition(t.id, first.next_state, first.note)
    ctx.service.transition(t.id, State.READY, "retry")
    second = ImplementStage().run(ctx.service.get(t.id), ctx)

    assert second.next_state is State.DOCUMENTING
    assert n["i"] == 2
    # The agent must see the operator's edit in its working tree on resume.
    assert fname in seen_files[1]


def test_rebase_conflict_blocks_on_resume(ctx_factory, tmp_path, monkeypatch):
    """When a WIP commit on the ticket branch conflicts with a newer
    remote commit, the resume rebase fails → REBASING with a note about
    rebase failure. The workspace is left intact for operator inspection."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )
    n = {"i": 0}

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        n["i"] += 1
        if n["i"] == 1:
            # Edit README.md to create a conflicting WIP commit.
            (Path(repo_dir) / "README.md").write_text("WIP edit to README\n")
            (Path(repo_dir) / "wip.txt").write_text("partial work")
            raise coding.AgentBudgetError("cap", [])
        # Should never reach here — the rebase should fail before the agent runs.
        raise AssertionError("agent should not run on resume when rebase fails")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "README.md", "wip.txt")

    first = ImplementStage().run(t, ctx)
    assert first.next_state is State.BLOCKED

    # Push a conflicting edit to README.md on the remote.
    _conflicting_edit_on_remote(remote, tmp_path)

    ctx.service.transition(t.id, first.next_state, first.note)
    ctx.service.transition(t.id, State.READY, "retry")

    second = ImplementStage().run(ctx.service.get(t.id), ctx)

    assert second.next_state is State.REBASING
    assert "rebase" in second.note.lower()
    assert n["i"] == 1  # agent only ran once (first pass); resume blocked before agent

    # Workspace left intact.
    ws = ctx.service.workspace(t)
    repo = ws.dir / "repo"
    assert (repo / ".git").exists()
    assert (repo / "wip.txt").exists()


def test_rebase_failure_on_fresh_clone_blocks(ctx_factory, tmp_path, monkeypatch):
    """When try_rebase_onto fails on a fresh clone (e.g. fetch error),
    the stage returns REBASING with a note about rebase failure."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )

    # Force try_rebase_onto to fail on the very first call (fresh clone path).
    orig_rebase = git_ops.try_rebase_onto
    call_count = [0]

    def _failing_rebase(repo, target):
        call_count[0] += 1
        if call_count[0] == 1:
            return False
        return orig_rebase(repo, target)

    monkeypatch.setattr(git_ops, "try_rebase_onto", _failing_rebase)

    agent_called = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        agent_called.append(1)
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.REBASING
    assert "rebase" in out.note.lower()
    assert len(agent_called) == 0  # agent never invoked


# --- dependency gating -------------------------------------------------

def test_unmet_dep_noops_at_ready(ctx_factory, tmp_path, monkeypatch):
    """Implement stage returns READY (no-op) when deps are unmet."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    # Create the dependency ticket (in DRAFT — not terminal)
    dep = ctx.service.create("Dep ticket")
    assert dep.state is State.DRAFT

    # Create the depender ticket
    t = ctx.service.create("Depender", depends_on=f'["{dep.id}"]')
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)

    agent_called = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        agent_called.append(1)
        (Path(repo_dir) / "out.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.READY  # same-state no-op
    assert len(agent_called) == 0          # agent NOT called
    assert out.note is None                # no note for no-op


def test_dep_satisfied_implement_proceeds(ctx_factory, tmp_path, monkeypatch):
    """Implement stage proceeds to DELIVERABLE when dep is CLOSED."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    # Create and close the dependency
    dep = ctx.service.create("Dep ticket")
    ctx.service.transition(dep.id, State.READY)
    ctx.service.transition(dep.id, State.DELIVERABLE)
    ctx.service.transition(dep.id, State.IMPLEMENT_COMPLETE)
    ctx.service.transition(dep.id, State.HUMAN_MR_APPROVAL)
    ctx.service.transition(dep.id, State.DONE)
    ctx.service.transition(dep.id, State.CLOSED)

    t = ctx.service.create("Depender", depends_on=f'["{dep.id}"]')
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        coding, "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING


def test_missing_dep_id_implement_proceeds(ctx_factory, tmp_path, monkeypatch):
    """Implement stage proceeds when a dep ID doesn't exist (treated satisfied)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    t = ctx.service.create("Depender", depends_on='["nonexistent-12345"]')
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        coding, "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


def test_no_deps_implement_proceeds_normally(ctx_factory, tmp_path, monkeypatch):
    """Tickets without depends_on have zero behavioral change."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    t = _ticket(ctx)  # creates ticket without depends_on
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        coding, "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


def test_success_to_code_review_when_review_enabled(ctx_factory, tmp_path, monkeypatch):
    """Pipeline flip: implement routes to CODE_REVIEW when review_enabled is true."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="true",
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.CODE_REVIEW


# --- epic context -------------------------------------------------------

def test_epic_context_prepended_to_spec(ctx_factory, tmp_path, monkeypatch):
    """When a ticket has an epic parent, the spec passed to
    run_implement_agent starts with the epic context wrapper."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",  # this test asserts the no-review path
    )

    # Create an epic with rich global context
    epic = ctx.service.create("Global Epic", "High-level goal: unify UX", kind="epic")
    # Create a child ticket under this epic
    child = ctx.service.create(
        "Add dark mode", "Please add dark mode toggle",
        parent_id=epic.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, feedback, reference_files, message_history, memory, epic_workspace_path
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(child, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(seen_spec) == 1
    expected = "<epic_context>\nHigh-level goal: unify UX\n</epic_context>"
    assert seen_spec[0].startswith(expected)


def test_epic_context_not_injected_without_epic_parent(ctx_factory, tmp_path, monkeypatch):
    """Ticket without a parent: no epic context in spec."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    t = _ticket(ctx, title="Standalone", body="Just a task")
    _write_file_map(ctx, t, "feature.txt")
    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, feedback, reference_files, message_history, memory, epic_workspace_path
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(t, ctx)
    assert len(seen_spec) == 1
    assert "<epic_context>" not in seen_spec[0]


def test_epic_context_not_injected_for_non_epic_parent(ctx_factory, tmp_path, monkeypatch):
    """Ticket with a parent that is NOT an epic: no epic context."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    # Create a regular task parent (kind="task")
    parent = ctx.service.create("Parent task", "Ordinary task", kind="task")
    child = ctx.service.create(
        "Child of task", "Do a sub-thing",
        parent_id=parent.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, feedback, reference_files, message_history, memory, epic_workspace_path
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(child, ctx)
    assert len(seen_spec) == 1
    assert "<epic_context>" not in seen_spec[0]


def test_epic_context_not_injected_for_empty_epic_description(ctx_factory, tmp_path, monkeypatch):
    """Epic with empty description: no injection."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")

    epic = ctx.service.create("Empty Epic", "", kind="epic")
    child = ctx.service.create(
        "Child of empty epic", "Do a thing",
        parent_id=epic.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, feedback, reference_files, message_history, memory, epic_workspace_path
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(child, ctx)
    assert len(seen_spec) == 1
    assert "<epic_context>" not in seen_spec[0]


# --- scope guardrail ----------------------------------------------------

def test_scope_violation_blocks_ticket(ctx_factory, tmp_path, monkeypatch):
    """When the agent modifies a tracked file not in file_map, the scope
    check catches it and immediately blocks the ticket — no retry.
    The test gate is never reached on the violating iteration."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="3",
        MILL_SCOPE_TRIAGE_ENABLED="false",
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

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        call_count["n"] += 1
        # Write wip.txt (in-scope) AND modify README.md (out-of-scope)
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "scope violation" in out.note
    assert call_count["n"] == 1, "agent must not be retried"
    assert (
        ctx.service.workspace(t).artifacts_dir / "implement.md"
    ).exists()


def test_scope_check_passes_when_all_in_scope(ctx_factory, tmp_path, monkeypatch, caplog):
    """When every changed file is in file_map, the scope check passes,
    logs an info message, and the loop proceeds to the test gate."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
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
        coding, "run_implement_agent",
        _fake_agent({"wip.txt": "done"}),
    )
    import logging

    with caplog.at_level(logging.INFO, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert any(
        "scope check passed" in m and "0 file(s) changed" in m
        for m in caplog.messages
    ), f"expected scope-passed info log, got: {caplog.messages}"


def test_scope_check_skipped_when_no_file_map(ctx_factory, tmp_path, monkeypatch, caplog):
    """When file_map.json is absent, the stage logs a warning and
    proceeds — scope enforcement is skipped, not blocked."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )
    t = _ticket(ctx)
    # No file_map.json written → stage should warn and proceed.

    agent_called = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        agent_called.append(1)
        (Path(repo_dir) / "out.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import logging

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING, f"expected DOCUMENTING, got {out.next_state}"
    assert len(agent_called) == 1, "agent must be called when file_map is missing"
    assert any(
        "skipping scope enforcement" in m for m in caplog.messages
    ), f"expected scope-skip warning, got: {caplog.messages}"


def test_scope_check_skipped_when_file_map_empty(ctx_factory, tmp_path, monkeypatch, caplog):
    """When file_map.json exists but is an empty array, the stage logs
    a warning and proceeds — same as a missing file_map."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text("[]", encoding="utf-8")

    agent_called = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        agent_called.append(1)
        (Path(repo_dir) / "out.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import logging

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING, f"expected DOCUMENTING, got {out.next_state}"
    assert len(agent_called) == 1, "agent must be called when file_map is empty"
    assert any(
        "skipping scope enforcement" in m for m in caplog.messages
    ), f"expected scope-skip warning, got: {caplog.messages}"


# --- scope-triage integration tests -------------------------------------


def test_scope_triage_expand_continues_loop(ctx_factory, tmp_path, monkeypatch):
    """EXPAND verdict: file_map is updated in-memory, the loop continues,
    and a comment is posted.  When at least one expand-file has *not*
    been modified yet, the agent is re-run (no retroactive short-circuit)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]', encoding="utf-8",
    )

    call_count = {"n": 0}

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(*, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries):
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
    # A scope-triage comment should be posted
    comments = ctx.service.list_comments(t.id)
    assert any("[scope-triage] EXPAND" in c.body for c in comments)


def test_scope_triage_expand_retroactive_short_circuit(ctx_factory, tmp_path, monkeypatch):
    """When scope-triage EXPANDs files that are *all* already in the
    current diff, the retroactive short-circuit fires: the agent is NOT
    re-run, the loop falls through to the test gate, and (with tests
    passing) the ticket finalizes without wasting an iteration."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]', encoding="utf-8",
    )

    call_count = {"n": 0}

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None,
             previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path, previous_attempt_summary
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("agent summary text", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(*, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="README.md is a natural side-effect edit",
            expand_files=["README.md"],
        )
    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    # Agent must be called exactly once (no wasted re-run).
    assert call_count["n"] == 1, (
        "retroactive short-circuit should skip agent re-run"
    )
    assert out.next_state is State.DOCUMENTING, (
        f"expected DOCUMENTING, got {out.next_state}"
    )
    # A [scope-triage] EXPAND comment is still posted.
    comments = ctx.service.list_comments(t.id)
    assert any("[scope-triage] EXPAND" in c.body for c in comments)
    # Outcome message is the agent's summary, not a scope-violation message.
    assert "agent summary text" in out.note


def test_scope_triage_reject_to_ready(ctx_factory, tmp_path, monkeypatch):
    """REJECT verdict: ticket goes back to READY with a comment naming
    the rogue files."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]', encoding="utf-8",
    )

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(*, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries):
        return ScopeTriageVerdict(
            action="REJECT",
            justification="Unrelated module — scope creep",
            expand_files=[],
        )
    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "REJECT" in out.note
    comments = ctx.service.list_comments(t.id)
    assert any("REJECT" in c.body and "scope-triage" in c.body for c in comments)


def test_scope_triage_escalate_to_blocked(ctx_factory, tmp_path, monkeypatch):
    """ESCALATE verdict: ticket goes to BLOCKED with triage reasoning."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]', encoding="utf-8",
    )

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(*, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries):
        return ScopeTriageVerdict(
            action="ESCALATE",
            justification="Ambiguous spec — cannot classify",
            expand_files=[],
        )
    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "ESCALATE" in out.note
    comments = ctx.service.list_comments(t.id)
    assert any("ESCALATE" in c.body and "scope-triage" in c.body for c in comments)


def test_scope_triage_disabled_falls_through(ctx_factory, tmp_path, monkeypatch):
    """When scope_triage_enabled=False, existing BLOCKED behaviour is
    preserved exactly — no triage agent is called."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="3",
        MILL_SCOPE_TRIAGE_ENABLED="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]', encoding="utf-8",
    )

    call_count = {"n": 0}

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "")

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
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]', encoding="utf-8",
    )

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    def _failing_triage(*, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _failing_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "agent error" in out.note
    comments = ctx.service.list_comments(t.id)
    assert any("agent error" in c.body for c in comments)


# --- post-edit reference_files persistence ------------------------------


def test_post_edit_reference_files_persisted(ctx_factory, tmp_path, monkeypatch):
    """After a successful agent pass, reference_files.json (paths-only,
    sourced from agent-curated list) and implement_summary.md are written
    to artifacts_dir."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )

    agent_called = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None,
             previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path, previous_attempt_summary
        agent_called.append(1)
        # Agent edits a file AND curates a list that includes an
        # additional file it didn't touch on disk — curated, not
        # git-derived.
        (Path(repo_dir) / "wip.txt").write_text("post-edit content here")
        return ("agent summary text", ["wip.txt", "base_class.py"], "")

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
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="false",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="2",
    )

    captured_refs: list[list[dict] | None] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None,
             previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, message_history, memory, epic_workspace_path, previous_attempt_summary
        captured_refs.append(reference_files)
        (Path(repo_dir) / "wip.txt").write_text("post-edit pass content")
        return ("agent summary", ["wip.txt"], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

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
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="false",
        MILL_REVIEW_ENABLED="false", MILL_MAX_FIX_ITERATIONS="2",
    )

    captured_feedback: list[str | None] = []
    captured_prev_summaries: list[str | None] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None,
             previous_attempt_summary=None, **_kwargs):
        del settings, spec, reference_files, message_history, memory, epic_workspace_path
        captured_feedback.append(feedback)
        captured_prev_summaries.append(previous_attempt_summary)
        (Path(repo_dir) / "wip.txt").write_text("edited")
        return ("pass-1-summary-abc", ["wip.txt"], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

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
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )

    agent_called = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None, previous_attempt_summary=None, **_kwargs):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        agent_called.append(1)
        (Path(repo_dir) / "out.txt").write_text("done")
        return ("summary", [], "")

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
    assert summary_path.exists(), (
        "implement_summary.md should exist from agent summary"
    )
