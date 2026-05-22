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
    ctx.service.transition(t.id, State.DOCUMENTING)
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
    ctx.service.transition(t.id, State.DOCUMENTING)
    ctx.service.transition(t.id, State.CODE_REVIEW)
    t = ctx.service.get(t.id)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "re-run implement" in out.note


# --- review.md artifact --------------------------------------------------

def test_writes_review_artifact_on_approve(ctx_factory, monkeypatch):
    """APPROVE with auto_merge_eligible=True → review.md exists."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(
            verdict="APPROVE", comments="lgtm", auto_merge_eligible=True,
        )

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    ReviewStage().run(t, ctx)
    artifact = ctx.service.workspace(t).artifacts_dir / "review.md"
    assert artifact.exists()
    text = artifact.read_text(encoding="utf-8")
    assert "verdict: APPROVE" in text
    assert "auto_merge_eligible: true" in text


def test_writes_review_artifact_on_request_changes(ctx_factory, monkeypatch):
    """REQUEST_CHANGES → review.md exists with auto_merge_eligible: false."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(
            verdict="REQUEST_CHANGES", comments="fix X",
            auto_merge_eligible=False,
        )

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    ReviewStage().run(t, ctx)
    artifact = ctx.service.workspace(t).artifacts_dir / "review.md"
    assert artifact.exists()
    text = artifact.read_text(encoding="utf-8")
    assert "verdict: REQUEST_CHANGES" in text
    assert "auto_merge_eligible: false" in text


def test_auto_merge_eligible_defaults_false(ctx_factory, monkeypatch):
    """When the model omits auto_merge_eligible, it defaults to False."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    # Simulate an agent response that only includes verdict + comments
    from pydantic import BaseModel

    class PartialVerdict(BaseModel):
        verdict: str = "APPROVE"
        comments: str = "ok"

    verdict = ReviewVerdict(**PartialVerdict().model_dump())
    assert verdict.auto_merge_eligible is False


# --- review round cap --------------------------------------------------

def test_request_changes_under_cap(ctx_factory, monkeypatch):
    """REQUEST_CHANGES with review_rounds < max → READY, counter incremented."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 1)  # 1 round already used
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(verdict="REQUEST_CHANGES", comments="fix X")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    # Counter incremented from 1 → 2
    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 2

    # Comment stored
    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "fix X"


def test_request_changes_at_cap_escalates(ctx_factory, monkeypatch):
    """When review_rounds hits the cap, REQUEST_CHANGES → DELIVERABLE."""
    ctx = ctx_factory(
        FORGE_REMOTE_URL="file:///dummy",
        MILL_REVIEW_ENABLED="true",
        MILL_REVIEW_MAX_ROUNDS="3",
    )
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 2)  # round 3 is the cap
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(verdict="REQUEST_CHANGES", comments="still broken")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert "exhausted" in out.note

    # Counter reset to 0
    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 0

    # Escalation comment stored
    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    assert "cap exhausted" in comments[0].body
    assert "3/3" in comments[0].body


def test_approve_resets_counter(ctx_factory, monkeypatch):
    """APPROVE resets review_rounds to 0."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 2)
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert "approved" in out.note

    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 0


def test_needs_discussion_preserves_counter(ctx_factory, monkeypatch):
    """NEEDS_DISCUSSION does NOT reset the review_rounds counter."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 1)
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(*, settings, diff, spec, model_name=None):
        del settings, diff, spec, model_name
        return ReviewVerdict(verdict="NEEDS_DISCUSSION", comments="questionable")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED

    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 1  # unchanged
