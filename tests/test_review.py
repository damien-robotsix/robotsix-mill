"""Tests for the review stage and review agent."""

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents.reviewing import ReviewVerdict, run_review_agent
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.review import ReviewStage


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_bare_repo(tmp_path: Path) -> str:
    """A throwaway local remote (file://) with a `main` branch."""
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
    return f"file://{bare}"


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    created = []

    def make(**env):
        db.reset_engine()
        s = Settings(MILL_DATA_DIR=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s)
        svc = TicketService(s)
        created.append(s)
        return StageContext(settings=s, service=svc)

    yield make
    db.reset_engine()


def _ticket(ctx, body="Add feature.txt"):
    t = ctx.service.create("Add feature", body)
    ctx.service.transition(t.id, State.READY)
    # Simulate implement having run: clone + branch + commit
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    remote = _make_bare_repo(ws.dir)
    _git(ws.dir, "clone", "-q", remote, str(repo_dir))
    _git(repo_dir, "config", "user.email", "mill@robotsix.local")
    _git(repo_dir, "config", "user.name", "robotsix-mill")
    _git(repo_dir, "checkout", "-q", "-B", f"mill/{t.id}")
    (repo_dir / "feature.txt").write_text("implemented")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "implement feature")
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    ctx.service.transition(t.id, State.CODE_REVIEW)
    return ctx.service.get(t.id)


# --- APPROVE -----------------------------------------------------------

def test_approve_transitions_to_deliverable(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert "approved" in out.note


# --- REQUEST_CHANGES ---------------------------------------------------

def test_request_changes_transitions_to_ready(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(verdict="REQUEST_CHANGES", comments="X is broken")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    # Comment was stored.
    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "X is broken"


# --- NEEDS_DISCUSSION --------------------------------------------------

def test_needs_discussion_transitions_to_blocked(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(verdict="NEEDS_DISCUSSION", comments="questionable design")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED

    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "questionable design"


# --- Blind review: diff + spec only, no implementation context --------

def test_blind_review_only_diff_and_spec(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    captured: dict = {}

    def _fake_review(*, settings, diff, spec, model_name=None):
        captured["diff"] = diff
        captured["spec"] = spec
        captured["model_name"] = model_name
        return ReviewVerdict(verdict="APPROVE", comments="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    ReviewStage().run(t, ctx)

    # diff contains the change
    assert "feature.txt" in captured["diff"]
    # spec is the ticket body
    assert captured["spec"] == "Add feature.txt"
    # no implementation memory leaked
    assert "memory" not in captured["diff"].lower()
    assert "ledger" not in captured["diff"].lower()


# --- Agent error → BLOCKED ---------------------------------------------

def test_agent_error_blocks_resumable(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note


# --- Empty diff → APPROVE without agent -------------------------------

def test_empty_diff_approves_without_agent(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    # Remove the commit so diff is empty.
    repo_dir = ctx.service.workspace(t).dir / "repo"
    _git(repo_dir, "reset", "--soft", "HEAD~1")

    agent_called = []

    def _fake_review(*, settings, diff, spec, model_name=None):
        agent_called.append(1)
        return ReviewVerdict(verdict="APPROVE", comments="")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert len(agent_called) == 0  # agent not called at all


# --- Missing repo guard → BLOCKED -------------------------------------

def test_missing_repo_blocks(ctx_factory):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = ctx.service.create("No clone")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.CODE_REVIEW)
    t = ctx.service.get(t.id)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "re-run implement" in out.note
