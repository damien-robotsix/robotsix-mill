"""Tests for the b92d loop-escalation guards.

Locks the review-feedback injection invariant: after a
review→ready bounce the reviewer's corrective comments must reach
the implement context (b92d spec item a).
"""

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.implement import ImplementStage


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_bare_repo(tmp_path: Path) -> str:
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
    import json as _json

    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "file_map.json").write_text(
        _json.dumps([{"file": f, "note": "test"} for f in files]),
        encoding="utf-8",
    )


# --- review-feedback injection invariant ---------------------------------


def test_feedback_injected_after_review_bounce(ctx_factory, tmp_path):
    """_load_implement_context includes open review feedback on a
    review→ready re-spawn — the reviewer's corrective comments must
    reach the next implement prompt (b92d spec item a)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 1)
    c = ctx.service.add_comment(
        t.id,
        "please fix the LICENSE check",
        author="reviewer",
    )

    t = ctx.service.get(t.id)
    ictx = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ictx.feedback is not None
    assert "please fix the LICENSE check" in ictx.feedback
    assert ictx.open_thread_ids == {c.id}


# --- gate-era comments are not implement feedback -------------------------


def test_gate_era_escalation_comment_dropped_after_approval(ctx_factory, tmp_path):
    """A chat drain's "ESCALATED to operator" hold posted while the ticket
    sat at human_issue_approval must NOT reach implement as feedback once
    the operator approves (human_issue_approval → ready) — 2026-10-23 six
    tickets read that hold as a test failure and closed DONE with empty
    diffs. A review comment posted after the approval still lands."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = ctx.service.create("Add feature", "Please add feature.txt")
    ctx.service.transition(t.id, State.HUMAN_ISSUE_APPROVAL)
    ctx.service.add_comment(
        t.id,
        "ESCALATED to operator 2026-10-22T20:07Z: lint class not in the "
        "allowlist. Do NOT approve until an operator answer is recorded.",
        author="robotsix-chat",
    )
    import time as _time

    _time.sleep(0.01)
    ctx.service.transition(t.id, State.READY, note="Operator approved Option B")
    _time.sleep(0.01)
    c = ctx.service.add_comment(t.id, "please fix the LICENSE check", author="reviewer")

    t = ctx.service.get(t.id)
    ictx = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ictx.feedback is not None
    assert "ESCALATED to operator" not in ictx.feedback
    assert "please fix the LICENSE check" in ictx.feedback
    assert ictx.open_thread_ids == {c.id}


def test_gate_era_comment_kept_without_approval_transition(ctx_factory, tmp_path):
    """Without a human_issue_approval → ready transition nothing is
    dropped — an ordinary review→ready bounce keeps every comment."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")
    t = _ticket(ctx)
    ctx.service.add_comment(t.id, "first remark", author="robotsix-chat")

    t = ctx.service.get(t.id)
    ictx = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ictx.feedback is not None
    assert "first remark" in ictx.feedback
