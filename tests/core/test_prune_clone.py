"""Tests for prune_clone helper and its integration with the retrospect stage."""

from __future__ import annotations

import shutil
from pathlib import Path


from robotsix_mill.agents import retrospecting
from robotsix_mill.agents.retrospecting import RetrospectResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace, prune_clone
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.retrospect import RetrospectStage


# ------------------------------------------------------------------ helpers
def _ctx(tmp_path: Path, **env_overrides):
    """Create a StageContext with Settings backed by *tmp_path* and optional
    environment variable overrides."""
    db.reset_engine()
    env = {
        "data_dir": str(tmp_path / "data"),
        "require_approval": False,
        "retrospect_spawn_drafts": False,
        "FORGE_KIND": "none",
    }
    env.update(env_overrides)
    s = Settings(**env)
    db.init_db(s, board_id="test-board")
    from robotsix_mill.core.service import TicketService
    from robotsix_mill.config import RepoConfig

    return StageContext(
        settings=s,
        service=TicketService(s, board_id="test-board"),
        repo_config=RepoConfig(
            repo_id="test-repo",
            
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
    )


def _done(ctx):
    """Create a ticket, move it to DONE, return the ticket."""
    t = ctx.service.create("prune test", "body")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.DONE,
    ):
        ctx.service.transition(t.id, st)
    return ctx.service.get(t.id)


def _default_result(**overrides):
    """Helper: build a RetrospectResult with required fields filled."""
    defaults = dict(
        findings="all good",
        conclusion="closed",
        updated_memory="",
        propose_draft=False,
        draft_title="",
        draft_body="",
    )
    defaults.update(overrides)
    return RetrospectResult(**defaults)


def _make_repo(ws: Workspace) -> Path:
    """Create a fake repo/ directory with some content."""
    ws.repo_dir.mkdir(parents=True, exist_ok=True)
    (ws.repo_dir / "README.md").write_text("repo content", encoding="utf-8")
    return ws.repo_dir


# ------------------------------------------------------------------- tests
class TestPruneCloneHelper:
    def test_removes_repo_directory(self, tmp_path):
        ws = Workspace(tmp_path / "workspaces", "t-1")
        _make_repo(ws)
        assert ws.repo_dir.exists()

        prune_clone(ws)
        assert not ws.repo_dir.exists()

    def test_noop_when_repo_absent(self, tmp_path):
        ws = Workspace(tmp_path / "workspaces", "t-2")
        # repo never created
        prune_clone(ws)  # should not raise
        assert not ws.repo_dir.exists()

    def test_swallows_deletion_error(self, tmp_path, monkeypatch):
        ws = Workspace(tmp_path / "workspaces", "t-3")
        _make_repo(ws)

        call_count = 0

        def failing_rmtree(path, ignore_errors=False):
            nonlocal call_count
            call_count += 1
            raise PermissionError("simulated permission error")

        monkeypatch.setattr(shutil, "rmtree", failing_rmtree)
        prune_clone(ws)

        # helper must not raise, and must attempt deletion
        assert call_count == 1

    def test_does_not_delete_description_or_artifacts(self, tmp_path):
        ws = Workspace(tmp_path / "workspaces", "t-4")
        ws.write_description("desc")
        (ws.artifacts_dir / "out.log").write_text("log", encoding="utf-8")
        _make_repo(ws)

        prune_clone(ws)

        assert ws.description_path.exists()
        assert (ws.artifacts_dir / "out.log").exists()


class TestPruneCloneIntegration:
    def test_closed_ticket_removes_repo(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path)
        ticket = _done(ctx)
        ws = ctx.service.workspace(ticket)
        _make_repo(ws)

        monkeypatch.setattr(
            retrospecting,
            "run_retrospect_agent",
            lambda **kwargs: _default_result(),
        )

        stage = RetrospectStage()
        out = stage.run(ticket, ctx)

        assert out.next_state is State.CLOSED
        assert not ws.repo_dir.exists()
        # description and artifacts survive
        assert ws.description_path.exists()
        assert ws.artifacts_dir.exists()
        assert (ws.artifacts_dir / "retrospect.md").exists()

    def test_setting_false_preserves_repo(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path, prune_clone_on_close="false")
        ticket = _done(ctx)
        ws = ctx.service.workspace(ticket)
        _make_repo(ws)

        monkeypatch.setattr(
            retrospecting,
            "run_retrospect_agent",
            lambda **kwargs: _default_result(),
        )

        stage = RetrospectStage()
        out = stage.run(ticket, ctx)

        assert out.next_state is State.CLOSED
        assert ws.repo_dir.exists()

    def test_deletion_error_still_closes_ticket(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path)
        ticket = _done(ctx)
        ws = ctx.service.workspace(ticket)
        _make_repo(ws)

        monkeypatch.setattr(
            retrospecting,
            "run_retrospect_agent",
            lambda **kwargs: _default_result(),
        )

        # Simulate a permission error during deletion.
        def fail_rmtree(path, ignore_errors=False):
            raise PermissionError("cannot remove")

        monkeypatch.setattr(shutil, "rmtree", fail_rmtree)

        stage = RetrospectStage()
        out = stage.run(ticket, ctx)

        # The ticket must still reach CLOSED, best-effort.
        assert out.next_state is State.CLOSED

    def test_repo_remains_for_non_closed_states(self, tmp_path, monkeypatch):
        """Test that repo/ is NOT removed for states other than closed."""
        ctx = _ctx(tmp_path)
        ticket = _done(ctx)
        ws = ctx.service.workspace(ticket)
        _make_repo(ws)

        # Don't call retrospect - just verify repo exists for DONE state
        assert ws.repo_dir.exists()

        # Transition to blocked (not closed) - repo should still exist
        ctx.service.transition(ticket.id, State.BLOCKED)
        assert ws.repo_dir.exists()

    def test_artifacts_intact_after_close(self, tmp_path, monkeypatch):
        """Test that artifacts/ directory is intact after closing."""
        ctx = _ctx(tmp_path)
        ticket = _done(ctx)
        ws = ctx.service.workspace(ticket)
        _make_repo(ws)

        # Add some artifacts
        (ws.artifacts_dir / "implement.md").write_text("impl", encoding="utf-8")
        (ws.artifacts_dir / "implement_messages.json").write_text(
            "{}", encoding="utf-8"
        )

        monkeypatch.setattr(
            retrospecting,
            "run_retrospect_agent",
            lambda **kwargs: _default_result(),
        )

        stage = RetrospectStage()
        out = stage.run(ticket, ctx)

        assert out.next_state is State.CLOSED
        assert (ws.artifacts_dir / "implement.md").exists()
        assert (ws.artifacts_dir / "implement_messages.json").exists()
        assert (ws.artifacts_dir / "retrospect.md").exists()
