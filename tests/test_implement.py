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
    def _run(*, settings, repo_dir, spec, feedback=None, history=None):
        del settings, spec, feedback, history  # signature must match the seam
        if write:
            for name, content in write.items():
                (Path(repo_dir) / name).write_text(content)
        return ("did the thing", [])

    return _run


# --- fs_tools sandbox ---------------------------------------------------

def test_fs_tools_roundtrip_and_sandbox(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    read_file, write_file, list_dir, run_command = build_fs_tools(tmp_path, s)
    assert "wrote" in write_file("a/b.txt", "hi")
    assert read_file("a/b.txt") == "hi"
    assert "a/" in list_dir(".")
    assert "exit=0" in run_command("echo ok")
    with pytest.raises(ValueError):
        read_file("../escape.txt")


# --- implement stage ----------------------------------------------------

def test_blocked_without_remote(ctx_factory):
    ctx = ctx_factory(MILL_TEST_COMMAND="true")
    out = ImplementStage().run(_ticket(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "FORGE_REMOTE_URL" in out.note


def test_success_to_in_review(ctx_factory, tmp_path, monkeypatch):
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true")
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.IN_REVIEW
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
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, MILL_TEST_COMMAND="true")
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent(None))
    out = ImplementStage().run(_ticket(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "no changes" in out.note


def test_test_failure_exhausts_then_blocks(ctx_factory, tmp_path, monkeypatch):
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        MILL_TEST_COMMAND="false",  # always fails
        MILL_MAX_FIX_ATTEMPTS="2",
    )
    calls = []

    def _run(*, settings, repo_dir, spec, feedback=None, history=None):
        del settings, spec, history  # signature must match the seam
        calls.append(feedback)
        (Path(repo_dir) / "wip.txt").write_text(f"attempt {len(calls)}")
        return ("tried", [])

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "after 2 attempts" in out.note
    assert len(calls) == 2  # bounded
    assert calls[0] is None and calls[1] is not None  # retry got feedback
    # WIP is committed so a human can pick it up
    repo = ctx.service.workspace(t).dir / "repo"
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True, text=True,
    ).stdout
    assert "WIP" in log
