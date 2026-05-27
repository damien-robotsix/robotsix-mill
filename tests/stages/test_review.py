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
        from robotsix_mill.config import RepoConfig; return StageContext(settings=s, service=svc, repo_config=RepoConfig(repo_id="test-repo", board_id="test-board", langfuse_project_name="test", langfuse_public_key="pk-test", langfuse_secret_key="sk-test"))

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
    # Pipeline flip: READY -> CODE_REVIEW directly (DOCUMENTING is now downstream).
    ctx.service.transition(t.id, State.CODE_REVIEW)
    return ctx.service.get(t.id)


# --- APPROVE -----------------------------------------------------------

def test_approve_transitions_to_deliverable(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert "approved" in out.note


# --- REQUEST_CHANGES ---------------------------------------------------

def test_request_changes_transitions_to_ready(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        agent_called.append(1)
        return ReviewVerdict(verdict="APPROVE", comments="")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 0  # agent not called at all


# --- Missing repo guard → BLOCKED -------------------------------------

def test_missing_repo_blocks(ctx_factory):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = ctx.service.create("No clone")
    ctx.service.transition(t.id, State.READY)
    # Pipeline flip: READY -> CODE_REVIEW directly.
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="REQUEST_CHANGES", comments="still broken")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
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

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert "approved" in out.note

    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 0


def test_needs_discussion_preserves_counter(ctx_factory, monkeypatch):
    """NEEDS_DISCUSSION does NOT reset the review_rounds counter."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 1)
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(*, settings, diff, spec, model_name=None, prior_context=None, repo_dir=None, reference_files=None):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="NEEDS_DISCUSSION", comments="questionable")

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED

    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 1  # unchanged


# --- Dependency spawning for out-of-scope asks -------------------------

import json

from robotsix_mill.agents.reviewing import ReviewAsk


def _write_file_map(ctx, ticket, files: list[str]) -> None:
    """Stamp the workspace's file_map.json — the same artifact refine
    writes. Each entry is ``{"file": <path>}``."""
    ws = ctx.service.workspace(ticket)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / "file_map.json").write_text(
        json.dumps([{"file": f} for f in files]),
        encoding="utf-8",
    )


def test_request_changes_in_scope_no_deps(ctx_factory, monkeypatch):
    """All asks touch files inside file_map → no dep tickets, single
    review comment, parent goes to READY (existing behaviour)."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="fix line 3 in feature.txt",
            request_changes=[ReviewAsk(
                description="Tighten the bounds check in feature.txt",
                files_touched=["feature.txt"],
            )],
        )

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    after = ctx.service.get(t.id)
    assert not after.depends_on  # no deps spawned
    # Only the original review comment exists (no dep-spawn notice).
    bodies = [c.body for c in ctx.service.list_comments(t.id)]
    assert any("fix line 3" in b for b in bodies)
    assert all("Spawned" not in b for b in bodies)


def test_request_changes_out_of_scope_spawns_dep_ticket(ctx_factory, monkeypatch):
    """Out-of-scope ask materialises a fresh ticket on the same board
    and the parent's depends_on is set so the worker's dep gate parks
    it until the new ticket closes."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])  # .gitignore is out-of-scope

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="add .gitignore for __pycache__",
            request_changes=[ReviewAsk(
                description="Add a .gitignore that excludes __pycache__",
                files_touched=[".gitignore"],
            )],
        )

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    after = ctx.service.get(t.id)
    deps = json.loads(after.depends_on or "[]")
    assert len(deps) == 1
    child = ctx.service.get(deps[0])
    assert child is not None
    assert child.source == "review"
    assert ".gitignore" in (child.body if hasattr(child, "body") else "") \
        or ".gitignore" in ctx.service.workspace(child).read_description()


def test_request_changes_mixed_scope_one_dep_one_in_scope(ctx_factory, monkeypatch):
    """Mixed verdict: in-scope asks stay on the parent (READY +
    comment), out-of-scope asks each spawn one dep ticket."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="two issues",
            request_changes=[
                ReviewAsk(
                    description="Tighten bounds in feature.txt",
                    files_touched=["feature.txt"],
                ),
                ReviewAsk(
                    description="Add a .gitignore for __pycache__",
                    files_touched=[".gitignore"],
                ),
            ],
        )

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    after = ctx.service.get(t.id)
    deps = json.loads(after.depends_on or "[]")
    assert len(deps) == 1  # only the out-of-scope ask spawned a dep


def test_request_changes_no_file_map_all_in_scope(ctx_factory, monkeypatch):
    """No file_map.json → every ask is treated as in-scope (legacy /
    scope-free flow). No deps spawned regardless of files_touched."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    # no file_map.json written

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="fix it",
            request_changes=[ReviewAsk(
                description="Add a .gitignore",
                files_touched=[".gitignore"],
            )],
        )

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    after = ctx.service.get(t.id)
    assert not after.depends_on


def test_out_of_scope_ask_uses_explicit_title(ctx_factory, monkeypatch):
    """When ReviewAsk.title is set, the spawned dependency ticket uses
    it verbatim — not a sentence cropped from the description. This is
    what stops the reviewer's symptom-framing ('remove
    __pycache__/foo.pyc') from becoming the new ticket's title when
    the proper fix is something else ('add __pycache__ to .gitignore')."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", MILL_REVIEW_ENABLED="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="add gitignore",
            request_changes=[ReviewAsk(
                title="Add __pycache__ to .gitignore",
                description=(
                    "__pycache__ files are tracked because the repo "
                    "has no .gitignore for compiled Python bytecode. "
                    "Add an entry for __pycache__/ to .gitignore."
                ),
                files_touched=[".gitignore"],
            )],
        )

    monkeypatch.setattr(
        "robotsix_mill.stages.review.run_review_agent", _fake_review
    )

    ReviewStage().run(t, ctx)

    after = ctx.service.get(t.id)
    deps = json.loads(after.depends_on or "[]")
    assert len(deps) == 1
    child = ctx.service.get(deps[0])
    assert child.title == "Add __pycache__ to .gitignore"
