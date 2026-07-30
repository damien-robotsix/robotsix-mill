"""Tests for the b92d loop-escalation guards.

Guard: a review re-spawn whose previous attempt committed only
changelog fragments (while review threads are open) escalates to
BLOCKED in preflight, before the agent loop, without consuming a
spawn — and the block note carries the reviewer's open gap list.

Also locks the review-feedback injection invariant: after a
review→ready bounce the reviewer's corrective comments must reach
the implement context (b92d spec item a).
"""

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
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
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, test_command="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 1)
    c = ctx.service.add_comment(
        t.id, "please fix the LICENSE check", author="reviewer"
    )

    t = ctx.service.get(t.id)
    ictx = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ictx.feedback is not None
    assert "please fix the LICENSE check" in ictx.feedback
    assert ictx.open_thread_ids == {c.id}


# --- changelog-only review re-spawn short-circuit ------------------------


def test_changelog_only_review_respawn_blocks_in_preflight(
    ctx_factory, tmp_path, monkeypatch
):
    """A review re-spawn whose previous attempt committed only changelog
    fragments (while review threads are open) blocks in preflight with
    the reviewer's gap list in the note — no spawn consumed."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="10",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None
    )
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _agent(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is not State.BLOCKED

    # Simulate the no-progress re-spawn: a changelog-only commit lands
    # on the branch while a review thread is still open.
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    frag = repo_dir / "changelog.d" / "+noop.bugfix.md"
    frag.parent.mkdir(exist_ok=True)
    frag.write_text("changelog-only no-op\n")
    _git(repo_dir, "add", "-A")
    _git(
        repo_dir,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "changelog only",
    )
    ctx.service.set_review_rounds(t.id, 1)
    c = ctx.service.add_comment(
        t.id, "gap: LICENSE check missing", author="reviewer"
    )

    t = ctx.service.get(t.id)
    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "changelog-only" in out.note.lower()
    assert "gap: LICENSE check missing" in out.note
    assert f"#{c.id}" in out.note


def test_real_code_respawn_not_blocked_by_changelog_guard(
    ctx_factory, tmp_path, monkeypatch
):
    """A review re-spawn whose last commit contains real code changes
    passes preflight — the guard only fires on changelog-only diffs."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="10",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None
    )
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _agent(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is not State.BLOCKED

    ctx.service.set_review_rounds(t.id, 1)
    ctx.service.add_comment(t.id, "minor: rename variable", author="reviewer")

    t = ctx.service.get(t.id)
    out = ImplementStage().preflight(t, ctx)

    # Last commit includes feature.txt (real code) → guard must not fire.
    assert out is None
