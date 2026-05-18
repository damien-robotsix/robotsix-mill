import subprocess
from pathlib import Path

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.deliver import DeliverStage
from robotsix_mill.vcs import git_ops


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def _bare(tmp_path) -> str:
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
        check=True, capture_output=True,
    )
    return f"file://{bare}", bare


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    s = Settings(**env)
    db.init_db(s)
    return StageContext(settings=s, service=TicketService(s))


def _ticket_with_branch(ctx, remote):
    """Simulate a finished implement: ticket in DELIVERABLE with a
    committed branch in its workspace clone."""
    t = ctx.service.create("add x", "do x")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    repo = ctx.service.workspace(t).dir / "repo"
    git_ops.clone(remote, repo, "main", None)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    (repo / "x.txt").write_text("done")
    git_ops.commit_all(repo, "impl")
    ctx.service.set_branch(t.id, branch)
    return ctx.service.get(t.id), branch


# --- owner/repo parsing -------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://github.com/damien-robotsix/robotsix-mill.git",
    "https://github.com/damien-robotsix/robotsix-mill",
    "git@github.com:damien-robotsix/robotsix-mill.git",
])
def test_parse_owner_repo(url):
    assert github._parse_owner_repo(url) == (
        "damien-robotsix", "robotsix-mill"
    )


def test_create_pr_posts_to_github_api(tmp_path, monkeypatch):
    import httpx

    calls = {}

    class FakeResp:
        status_code = 201
        text = ""

        def json(self):
            return {"html_url": "https://github.com/o/r/pull/7"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.update(url=url, headers=headers, json=json)
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    s = Settings(
        MILL_DATA_DIR=str(tmp_path), FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git", FORGE_TOKEN="tok",
    )
    url = github.GitHubForge(s).open_merge_request(
        source_branch="mill/x", title="T", body="B"
    )
    assert url == "https://github.com/o/r/pull/7"
    assert calls["url"].endswith("/repos/o/r/pulls")
    assert calls["json"] == {
        "title": "T", "head": "mill/x", "base": "main", "body": "B",
    }
    assert calls["headers"]["Authorization"] == "Bearer tok"


# --- deliver guards (no external calls) --------------------------------

def test_blocked_without_forge_kind(tmp_path):
    ctx = _ctx(tmp_path, MILL_DATA_DIR=str(tmp_path / "d0"))
    t = ctx.service.create("x", "y")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    out = DeliverStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.BLOCKED and "FORGE_KIND" in out.note


def test_blocked_without_token(tmp_path):
    remote, _ = _bare(tmp_path)
    ctx = _ctx(tmp_path, FORGE_KIND="github", FORGE_REMOTE_URL=remote)
    t = ctx.service.create("x", "y")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    out = DeliverStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.BLOCKED and "FORGE_TOKEN" in out.note


def test_blocked_without_branch(tmp_path):
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path, FORGE_KIND="github",
        FORGE_REMOTE_URL=remote, FORGE_TOKEN="t",
    )
    t = ctx.service.create("x", "y")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    out = DeliverStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.BLOCKED and "no implemented branch" in out.note


# --- success + resumable failure ---------------------------------------

def test_success_pushes_and_opens_pr(tmp_path, monkeypatch):
    remote, bare = _bare(tmp_path)
    ctx = _ctx(
        tmp_path, FORGE_KIND="github",
        FORGE_REMOTE_URL=remote, FORGE_TOKEN="t",
    )
    seen = {}

    def fake_pr(self, *, source_branch, title, body):
        seen.update(source_branch=source_branch, title=title)
        return "https://github.com/o/r/pull/42"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)
    t, branch = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "https://github.com/o/r/pull/42" in out.note
    assert seen["source_branch"] == branch
    assert t.id in seen["title"]
    # branch actually pushed to the bare remote
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        capture_output=True, text=True,
    ).stdout
    assert branch in refs
    assert (ctx.service.workspace(t).artifacts_dir / "deliver.md").exists()


def test_pr_api_error_blocks_resumable(tmp_path, monkeypatch):
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path, FORGE_KIND="github",
        FORGE_REMOTE_URL=remote, FORGE_TOKEN="t",
    )

    def boom(self, *, source_branch, title, body):
        raise RuntimeError("GitHub PR create failed: 403")

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", boom)
    t, _ = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note and "403" in out.note
