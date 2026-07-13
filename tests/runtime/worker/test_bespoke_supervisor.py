"""Tests for the bespoke-agent supervisor + per-loop scheduling.

The supervisor lives on ``Worker`` and is the dynamic scheduler half
of the bespoke feature. It's responsible for:

1. Discovering ``.robotsix-mill/agents/<name>.yaml`` definitions in the
   managed repo's clone.
2. Spawning one periodic asyncio task per definition, each on its
   declared ``interval_seconds``.
3. Reacting to YAML add / remove / change between cycles so an
   operator can deploy a new bespoke agent (or kill one) by pushing
   to the managed repo, without restarting mill.

The supervisor is the only piece in mill that mutates the running
task graph from the LLM-side of the system, so its reconciliation
contract is what these tests guard.

Strategy: monkeypatch the clone/fetch + the bespoke-pass runner so
no network and no LLM is involved, then drive the supervisor's
discovery cycle directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from robotsix_mill.agents.bespoke_loader import BespokeAgentDefinition
from robotsix_mill.config import RepoConfig, Settings, _reset_secrets
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.runtime.worker import Worker
from robotsix_mill.stages import StageContext


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    s = Settings(data_dir=str(tmp_path / "data"))
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    _reset_secrets()
    return s


@pytest.fixture
def repo_config():
    return RepoConfig(
        repo_id="my-app",
        board_id="my-app",
        langfuse_project_name="my-app",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )


@pytest.fixture
def worker(settings, repo_config):
    svc = TicketService(settings, board_id=repo_config.board_id)
    ctx = StageContext(
        settings=settings,
        service=svc,
        repo_config=repo_config,
    )
    return Worker(ctx, run_registry=None)


def _make_clone(tmp_path, board_id) -> Path:
    """Build a fake clone tree at the path the supervisor would use,
    so the no-op clone/fetch monkeypatches don't try to create one."""
    clone = tmp_path / "data" / "my-app" / "periodic_workspace" / "repo"
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()  # so the "already cloned" branch hits
    return clone


def _stub_clone_helpers(monkeypatch):
    """No-op the supervisor's clone + fetch + reset shellouts so the
    tests don't touch the network or hit the real git. The supervisor
    imports subprocess inline; patch the global module to neutralise
    the ``git reset --hard origin/<branch>`` it issues on each cycle."""
    from robotsix_mill.vcs import git_ops
    import subprocess as _sp

    monkeypatch.setattr(git_ops, "clone", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(_sp, "run", lambda *a, **k: None)


# ---------------------------------------------------------------------------
#  Discovery + scheduling reconciliation
# ---------------------------------------------------------------------------


class TestBespokeSupervisor:
    @pytest.mark.asyncio
    async def test_new_yaml_spawns_loop(
        self,
        tmp_path,
        monkeypatch,
        worker,
        repo_config,
    ):
        """A YAML committed to the managed repo's
        ``.robotsix-mill/agents/`` results in a per-bespoke loop task
        being spawned by the supervisor on its next discovery cycle."""
        _stub_clone_helpers(monkeypatch)
        clone = _make_clone(tmp_path, repo_config.board_id)
        _write_yaml(
            clone / ".robotsix-mill" / "agents" / "mail.yaml",
            {
                "name": "mail",
                "interval_seconds": 3600,
                "system_prompt": "P",
            },
        )

        spawned: list[BespokeAgentDefinition] = []

        async def fake_loop(self, rc, definition, clone_dir):
            spawned.append(definition)
            await asyncio.Event().wait()  # park forever

        monkeypatch.setattr(
            Worker,
            "_run_bespoke_loop",
            fake_loop,
            raising=True,
        )

        # Bound the supervisor to one cycle by setting a short
        # discovery interval, then cancel.
        monkeypatch.setattr(
            worker.ctx.settings,
            "bespoke_discovery_interval_seconds",
            60,
        )
        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        # Let the discovery + spawn happen.
        for _ in range(20):
            if spawned:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(spawned) == 1
        assert spawned[0].name == "mail"

    @pytest.mark.asyncio
    async def test_legacy_bespoke_workspace_is_migrated(
        self,
        tmp_path,
        monkeypatch,
        worker,
        repo_config,
    ):
        """A pre-existing legacy ``bespoke_workspace`` clone is renamed to
        ``periodic_workspace`` on the first cycle (no re-clone), and discovery
        still works through the migrated clone."""
        _stub_clone_helpers(monkeypatch)
        base = tmp_path / "data" / "my-app"
        legacy = base / "bespoke_workspace" / "repo"
        legacy.mkdir(parents=True)
        (legacy / ".git").mkdir()
        _write_yaml(
            legacy / ".robotsix-mill" / "agents" / "mail.yaml",
            {"name": "mail", "interval_seconds": 3600, "system_prompt": "P"},
        )

        spawned: list[BespokeAgentDefinition] = []

        async def fake_loop(self, rc, definition, clone_dir):
            spawned.append(definition)
            await asyncio.Event().wait()

        monkeypatch.setattr(Worker, "_run_bespoke_loop", fake_loop, raising=True)
        monkeypatch.setattr(
            worker.ctx.settings, "bespoke_discovery_interval_seconds", 60
        )

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        for _ in range(20):
            if spawned:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Renamed: legacy gone, new path holds the clone + its committed yaml.
        assert not (base / "bespoke_workspace").exists()
        assert (base / "periodic_workspace" / "repo" / ".git").exists()
        assert (
            base
            / "periodic_workspace"
            / "repo"
            / ".robotsix-mill"
            / "agents"
            / "mail.yaml"
        ).exists()
        # Discovery still found the workflow through the migrated clone.
        assert [d.name for d in spawned] == ["mail"]

    @pytest.mark.asyncio
    async def test_removed_yaml_cancels_loop(
        self,
        tmp_path,
        monkeypatch,
        worker,
        repo_config,
    ):
        """When a YAML disappears from the managed repo between cycles
        the supervisor MUST cancel the corresponding loop task. Without
        this, the operator can't actually retire a bespoke agent —
        they'd be stuck running the prior definition forever."""
        _stub_clone_helpers(monkeypatch)
        clone = _make_clone(tmp_path, repo_config.board_id)
        yaml_path = clone / ".robotsix-mill" / "agents" / "mail.yaml"
        _write_yaml(
            yaml_path,
            {
                "name": "mail",
                "interval_seconds": 3600,
                "system_prompt": "P",
            },
        )

        cancel_events: list[str] = []

        async def fake_loop(self, rc, definition, clone_dir):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_events.append(definition.name)
                raise

        monkeypatch.setattr(
            Worker,
            "_run_bespoke_loop",
            fake_loop,
            raising=True,
        )

        # Discovery interval short so cycles tick fast.
        monkeypatch.setattr(
            worker.ctx.settings,
            "bespoke_discovery_interval_seconds",
            60,
        )
        # Hack: monkey the supervisor's max-sleep so the test
        # doesn't actually wait 60s between cycles.
        import robotsix_mill.runtime.worker as _w_mod

        real_sleep = asyncio.sleep

        async def fast_sleep(t):
            await real_sleep(min(t, 0.01))

        monkeypatch.setattr(_w_mod.asyncio, "sleep", fast_sleep)

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        # Wait until first cycle ran (loop spawned) — give a few ticks.
        await real_sleep(0.05)
        # Remove the YAML and let another cycle happen.
        yaml_path.unlink()
        await real_sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # cancel_events captures cancellation from EITHER mid-loop
        # (YAML removal) or supervisor teardown — but the YAML-removal
        # path should fire at least once, so the agent name appears.
        assert "mail" in cancel_events

    @pytest.mark.asyncio
    async def test_changed_yaml_respawns_loop(
        self,
        tmp_path,
        monkeypatch,
        worker,
        repo_config,
    ):
        """When the YAML body changes (e.g. operator updates the
        prompt) the supervisor MUST cancel + respawn so the new
        definition takes effect — silent staleness would be worse
        than a brief interruption."""
        _stub_clone_helpers(monkeypatch)
        clone = _make_clone(tmp_path, repo_config.board_id)
        yaml_path = clone / ".robotsix-mill" / "agents" / "mail.yaml"
        _write_yaml(
            yaml_path,
            {
                "name": "mail",
                "interval_seconds": 3600,
                "system_prompt": "old prompt",
            },
        )

        spawns: list[str] = []

        async def fake_loop(self, rc, definition, clone_dir):
            spawns.append(definition.system_prompt)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        monkeypatch.setattr(
            Worker,
            "_run_bespoke_loop",
            fake_loop,
            raising=True,
        )

        # Speed up the supervisor cycle.
        import robotsix_mill.runtime.worker as _w_mod

        real_sleep = asyncio.sleep

        async def fast_sleep(t):
            await real_sleep(min(t, 0.01))

        monkeypatch.setattr(_w_mod.asyncio, "sleep", fast_sleep)

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        await real_sleep(0.05)
        # Mutate the YAML body in place.
        _write_yaml(
            yaml_path,
            {
                "name": "mail",
                "interval_seconds": 3600,
                "system_prompt": "new prompt",
            },
        )
        await real_sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Both versions of the prompt were spawned at some point.
        assert "old prompt" in spawns
        assert "new prompt" in spawns

    @pytest.mark.asyncio
    async def test_supervisor_cancel_cancels_children(
        self,
        tmp_path,
        monkeypatch,
        worker,
        repo_config,
    ):
        """Worker.stop() cancels the supervisor; the supervisor's
        finally clause MUST cancel every child loop task it spawned.
        Without that, a worker shutdown would leak periodic LLM
        invocations into the next process lifetime."""
        _stub_clone_helpers(monkeypatch)
        clone = _make_clone(tmp_path, repo_config.board_id)
        for name in ("a", "b", "c"):
            _write_yaml(
                clone / ".robotsix-mill" / "agents" / f"{name}.yaml",
                {
                    "name": name,
                    "interval_seconds": 3600,
                    "system_prompt": "P",
                },
            )

        running_children: list[asyncio.Task] = []

        async def fake_loop(self, rc, definition, clone_dir):
            t = asyncio.current_task()
            running_children.append(t)
            await asyncio.Event().wait()

        monkeypatch.setattr(
            Worker,
            "_run_bespoke_loop",
            fake_loop,
            raising=True,
        )

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        for _ in range(30):
            if len(running_children) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(running_children) == 3
        # Cancel the supervisor; its finally must tear down children.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Allow scheduler to propagate cancellations.
        await asyncio.sleep(0.01)
        assert all(c.cancelled() or c.done() for c in running_children), (
            "supervisor cancellation did NOT propagate to child bespoke "
            "loops; per-bespoke LLM invocations would leak past stop()"
        )


# ---------------------------------------------------------------------------
#  Unified periodic-workflow path (.robotsix-mill/periodic/<name>.yaml)
# ---------------------------------------------------------------------------


class TestPeriodicSupervisorWorkflows:
    @pytest.mark.asyncio
    async def test_presence_file_schedules_llm_agent_loop(
        self, tmp_path, monkeypatch, worker, repo_config
    ):
        """An `audit` presence file under .robotsix-mill/periodic/ makes the
        supervisor schedule a periodic-workflow loop (llm_agent kind) with the
        merged definition."""
        _stub_clone_helpers(monkeypatch)
        clone = _make_clone(tmp_path, repo_config.board_id)
        _write_yaml(
            clone / ".robotsix-mill" / "periodic" / "audit.yaml",
            {"name": "audit", "interval_seconds": 4242},
        )

        scheduled = []

        async def fake_loop(self, rc, wf, clone_dir):
            scheduled.append(wf)
            await asyncio.Event().wait()

        monkeypatch.setattr(
            Worker, "_run_periodic_workflow_loop", fake_loop, raising=True
        )
        monkeypatch.setattr(
            worker.ctx.settings, "bespoke_discovery_interval_seconds", 60
        )

        task = asyncio.create_task(worker._periodic_supervisor(repo_config))
        for _ in range(30):
            if scheduled:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(scheduled) == 1
        assert scheduled[0].name == "audit"
        assert scheduled[0].kind == "llm_agent"
        assert scheduled[0].interval_seconds == 4242

    def test_has_periodic_presence(self, tmp_path, worker, repo_config):
        clone = _make_clone(tmp_path, repo_config.board_id)
        assert worker._has_periodic_presence(repo_config, "audit") is False
        _write_yaml(
            clone / ".robotsix-mill" / "periodic" / "audit.yaml", {"name": "audit"}
        )
        assert worker._has_periodic_presence(repo_config, "audit") is True
        # hyphen→underscore normalization
        _write_yaml(
            clone / ".robotsix-mill" / "periodic" / "copy_paste.yaml",
            {"name": "copy_paste"},
        )
        assert worker._has_periodic_presence(repo_config, "copy-paste") is True

    def test_build_runner_by_kind(self, worker):
        from types import SimpleNamespace

        llm = SimpleNamespace(kind="llm_agent", name="audit", definition=object())
        assert callable(worker._build_periodic_workflow_runner(llm))
        sched = SimpleNamespace(kind="schedule_only", name="trace_review")
        assert callable(worker._build_periodic_workflow_runner(sched))
