"""Tests for the member-sync event-trigger wired into the periodic
supervisor.

The supervisor fires member-sync out-of-band when a managed repo's
``repos.yaml`` content hash changes between cycles. There is no
file-watch infrastructure — change detection is poll-cycle content-hash
only, persisted at ``<data_dir>/<repo_id>/member_sync_repos_hash``.

Strategy mirrors ``test_bespoke_supervisor``: stub the clone/fetch
shellouts and ``_fire_periodic_pass`` so no network / LLM is involved,
then drive the supervisor's discovery cycle directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from robotsix_mill.config import RepoConfig, Settings, _reset_secrets
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.runtime.worker import Worker
from robotsix_mill.runtime.worker.periodic_passes import (
    _hash_repos_yaml,
    _load_repos_yaml_hash,
    _save_repos_yaml_hash,
)
from robotsix_mill.stages import StageContext


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    s = Settings(data_dir=str(tmp_path / "data"))
    db.reset_engine()
    db.init_db(s, board_id="my-app")
    _reset_secrets()
    return s


@pytest.fixture
def repo_config():
    return RepoConfig(
        repo_id="my-app",
        
        langfuse_project_name="my-app",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )


@pytest.fixture
def worker(settings, repo_config):
    svc = TicketService(settings, board_id=repo_config.board_id)
    ctx = StageContext(settings=settings, service=svc, repo_config=repo_config)
    return Worker(ctx, run_registry=None)


def _make_clone(tmp_path) -> Path:
    clone = tmp_path / "data" / "my-app" / "periodic_workspace" / "repo"
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()
    return clone


def _stub_clone_helpers(monkeypatch):
    from robotsix_mill.vcs import git_ops
    import subprocess as _sp

    monkeypatch.setattr(git_ops, "clone", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(_sp, "run", lambda *a, **k: None)


def _isolate_scheduling(monkeypatch):
    """Park the workflow/bespoke loops so the only thing the supervisor
    does that we observe is the member-sync event trigger."""

    async def _park(self, *a, **k):
        await asyncio.Event().wait()

    monkeypatch.setattr(Worker, "_run_periodic_workflow_loop", _park, raising=True)
    monkeypatch.setattr(Worker, "_run_bespoke_loop", _park, raising=True)


# ---------------------------------------------------------------------------
# Hash helper unit tests
# ---------------------------------------------------------------------------


class TestHashHelpers:
    def test_missing_manifest_hashes_to_sentinel(self, tmp_path):
        assert _hash_repos_yaml(tmp_path) == ""

    def test_hash_changes_with_content(self, tmp_path):
        (tmp_path / "repos.yaml").write_text("a: 1\n", encoding="utf-8")
        h1 = _hash_repos_yaml(tmp_path)
        assert h1
        (tmp_path / "repos.yaml").write_text("a: 2\n", encoding="utf-8")
        assert _hash_repos_yaml(tmp_path) != h1

    def test_save_then_load_roundtrip(self, settings):
        assert _load_repos_yaml_hash(settings, "my-app") == ""
        _save_repos_yaml_hash(settings, "my-app", "deadbeef")
        assert _load_repos_yaml_hash(settings, "my-app") == "deadbeef"
        # Persisted at the documented path.
        p = settings.data_dir / "my-app" / "member_sync_repos_hash"
        assert p.exists()


# ---------------------------------------------------------------------------
# Supervisor event-trigger
# ---------------------------------------------------------------------------


class TestMemberSyncTrigger:
    @pytest.mark.asyncio
    async def test_changed_manifest_fires_once_then_quiesces(
        self, tmp_path, monkeypatch, worker, repo_config
    ):
        _stub_clone_helpers(monkeypatch)
        _isolate_scheduling(monkeypatch)
        clone = _make_clone(tmp_path)
        (clone / "repos.yaml").write_text("repositories: {}\n", encoding="utf-8")

        fired: list[str] = []

        async def fake_fire(self, label, runner_fn, rc):
            fired.append(label)

        monkeypatch.setattr(Worker, "_fire_periodic_pass", fake_fire, raising=True)

        import robotsix_mill.runtime.worker.periodic_passes as _pp

        real_sleep = asyncio.sleep

        async def fast_sleep(t):
            await real_sleep(min(t, 0.01))

        monkeypatch.setattr(_pp.asyncio, "sleep", fast_sleep)

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        # Several cycles with the SAME content: should fire exactly once.
        await real_sleep(0.06)
        assert fired == ["member_sync"]

        # Mutate the manifest → fire again on the next cycle.
        (clone / "repos.yaml").write_text(
            "repositories:\n  src/a:\n    url: https://x/a.git\n",
            encoding="utf-8",
        )
        await real_sleep(0.06)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert fired == ["member_sync", "member_sync"]
        # Hash persisted after the fire.
        assert _load_repos_yaml_hash(worker.ctx.settings, "my-app") != ""

    @pytest.mark.asyncio
    async def test_no_manifest_never_fires(
        self, tmp_path, monkeypatch, worker, repo_config
    ):
        _stub_clone_helpers(monkeypatch)
        _isolate_scheduling(monkeypatch)
        _make_clone(tmp_path)  # clone WITHOUT a repos.yaml

        fired: list[str] = []

        async def fake_fire(self, label, runner_fn, rc):
            fired.append(label)

        monkeypatch.setattr(Worker, "_fire_periodic_pass", fake_fire, raising=True)

        import robotsix_mill.runtime.worker.periodic_passes as _pp

        real_sleep = asyncio.sleep

        async def fast_sleep(t):
            await real_sleep(min(t, 0.01))

        monkeypatch.setattr(_pp.asyncio, "sleep", fast_sleep)

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        await real_sleep(0.06)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert fired == []

    @pytest.mark.asyncio
    async def test_disabled_flag_never_fires(
        self, tmp_path, monkeypatch, worker, repo_config
    ):
        _stub_clone_helpers(monkeypatch)
        _isolate_scheduling(monkeypatch)
        clone = _make_clone(tmp_path)
        (clone / "repos.yaml").write_text("repositories: {}\n", encoding="utf-8")
        monkeypatch.setattr(
            worker.ctx.settings, "member_sync_periodic", False, raising=False
        )

        fired: list[str] = []

        async def fake_fire(self, label, runner_fn, rc):
            fired.append(label)

        monkeypatch.setattr(Worker, "_fire_periodic_pass", fake_fire, raising=True)

        import robotsix_mill.runtime.worker.periodic_passes as _pp

        real_sleep = asyncio.sleep

        async def fast_sleep(t):
            await real_sleep(min(t, 0.01))

        monkeypatch.setattr(_pp.asyncio, "sleep", fast_sleep)

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        await real_sleep(0.06)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert fired == []
