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
        return StageContext(settings=s, service=svc)

    yield make
    db.reset_engine()


def _ticket(ctx, title="Add feature", body="Please add feature.txt"):
    t = ctx.service.create(title, body)
    ctx.service.transition(t.id, State.READY)
    return ctx.service.get(t.id)


def _fake_agent(write: dict | None):
    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path  # signature must match the seam
        if write:
            for name, content in write.items():
                (Path(repo_dir) / name).write_text(content)
        return ("did the thing", [], "")

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
    out = ImplementStage().run(_ticket(ctx), ctx)
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

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
        del settings, spec, reference_files, message_history, memory, epic_workspace_path  # seam signature
        calls.append(feedback)
        (Path(repo_dir) / "wip.txt").write_text("did work")
        return ("tried", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)

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

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        (Path(repo_dir) / "partial.txt").write_text("half done")
        raise coding.AgentBudgetError("request_limit of 50", [])

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note and "budget" in out.note
    # WIP committed so a human can resume (no transcript now — a resume
    # re-runs the coordinator fresh).
    ws = ctx.service.workspace(t)
    assert "WIP" in _commits(ws.dir / "repo")[0]


def test_resume_reruns_coordinator_without_reclone(ctx_factory, tmp_path, monkeypatch):
    """Resume = run the coordinator FRESH (no transcript replay), and
    crucially do NOT re-clone — the prior WIP branch is reused."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true", MILL_REVIEW_ENABLED="false")
    n = {"i": 0}

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        n["i"] += 1
        if n["i"] == 1:  # first pass: partial work, hit the cap
            (Path(repo_dir) / "first.txt").write_text("1")
            raise coding.AgentBudgetError("cap", [])
        (Path(repo_dir) / "second.txt").write_text("2")
        return ("finished on resume", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

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
             message_history=None, memory="", epic_workspace_path=None):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        # Record what the agent can see in the working tree.
        for p in sorted(Path(repo_dir).iterdir()):
            if p.name != ".git":
                seen_files.append(p.name)
        (Path(repo_dir) / "agent_out.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

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
             message_history=None, memory="", epic_workspace_path=None):
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
    remote commit, the resume rebase fails → BLOCKED with a note about
    rebase failure.  The workspace is left intact for operator inspection."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true",
        MILL_REVIEW_ENABLED="false",
    )
    n = {"i": 0}

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None,
             message_history=None, memory="", epic_workspace_path=None):
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

    first = ImplementStage().run(t, ctx)
    assert first.next_state is State.BLOCKED

    # Push a conflicting edit to README.md on the remote.
    _conflicting_edit_on_remote(remote, tmp_path)

    ctx.service.transition(t.id, first.next_state, first.note)
    ctx.service.transition(t.id, State.READY, "retry")

    second = ImplementStage().run(ctx.service.get(t.id), ctx)

    assert second.next_state is State.BLOCKED
    assert "rebase" in second.note.lower()
    assert n["i"] == 1  # agent only ran once (first pass); resume blocked before agent

    # Workspace left intact.
    ws = ctx.service.workspace(t)
    repo = ws.dir / "repo"
    assert (repo / ".git").exists()
    assert (repo / "wip.txt").exists()


def test_rebase_failure_on_fresh_clone_blocks(ctx_factory, tmp_path, monkeypatch):
    """When try_rebase_onto fails on a fresh clone (e.g. fetch error),
    the stage returns BLOCKED with a note about rebase failure."""
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
             message_history=None, memory="", epic_workspace_path=None):
        del settings, spec, feedback, reference_files, message_history, memory, epic_workspace_path
        agent_called.append(1)
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
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

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
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
    ctx.service.transition(dep.id, State.HUMAN_MR_APPROVAL)
    ctx.service.transition(dep.id, State.DONE)
    ctx.service.transition(dep.id, State.CLOSED)

    t = ctx.service.create("Depender", depends_on=f'["{dep.id}"]')
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)

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

    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
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
    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
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

    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
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

    seen_spec: list[str] = []

    def _run(*, settings, repo_dir, spec, feedback=None, reference_files=None, message_history=None, memory="", epic_workspace_path=None):
        del settings, feedback, reference_files, message_history, memory, epic_workspace_path
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(child, ctx)
    assert len(seen_spec) == 1
    assert "<epic_context>" not in seen_spec[0]
