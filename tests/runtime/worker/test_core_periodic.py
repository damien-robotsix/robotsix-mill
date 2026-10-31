import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from robotsix_mill.runtime.worker import Worker
from robotsix_mill.stages import StageContext


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


# --- startup-aware periodic pass (last-run aware) ----------------------


def test_initial_delay_fires_soon_when_overdue(ctx, tmp_path):
    """The periodic cadence brain (_initial_delay, used by the supervisor's
    per-workflow loops): a RunRegistry entry older than the interval → fire
    almost immediately (~1s base + per-kind stagger), not after a full
    interval."""
    import json
    from datetime import timedelta

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    db_path = tmp_path / "runs.json"
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    db_path.write_text(
        json.dumps(
            [
                {
                    "id": "a1",
                    "kind": "audit",
                    "started_at": old,
                    "finished_at": old,
                    "status": "ok",
                    "summary": "",
                    "error": None,
                }
            ]
        )
    )
    w = Worker(ctx, run_registry=RunRegistry(db_path))
    delay = w._initial_delay("audit", 86400)
    # Base 1.0 + deterministic stagger (hash("audit") % 3600 ≈ 289)
    # + random jitter 0..60.  Assert a generous range.
    assert 1.0 <= delay <= 420


def test_initial_delay_waits_when_recent(ctx, tmp_path):
    """A recent RunRegistry entry → _initial_delay returns the remaining
    interval (close to the full interval) plus per-kind stagger, so the
    loop does NOT re-fire now."""
    import json

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    db_path = tmp_path / "runs.json"
    recent = datetime.now(UTC).isoformat()
    db_path.write_text(
        json.dumps(
            [
                {
                    "id": "a1",
                    "kind": "audit",
                    "started_at": recent,
                    "finished_at": recent,
                    "status": "ok",
                    "summary": "",
                    "error": None,
                }
            ]
        )
    )
    w = Worker(ctx, run_registry=RunRegistry(db_path))
    delay = w._initial_delay("audit", 86400)
    # ~86400 remaining + deterministic stagger + random jitter.
    assert 86000 < delay <= 86800


def test_orphaned_pr_check_interval_persists_across_restart(ctx, tmp_path):
    """A completed orphaned-PR-check pass is recorded in the run registry, so a
    worker restart does NOT re-fire the pass the same day.

    Regression: the loop never recorded a run entry, so _initial_delay found no
    prior run and scheduled the next pass ~immediately after every restart —
    causing a second daily pass that refiled a mark-done'd tracker (#3158).
    """
    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    db_path = tmp_path / "runs.json"
    reg = RunRegistry(db_path)
    # Simulate one completed pass (as the loop now does).
    run_id = reg.start("orphaned-pr-check")
    reg.finish_ok(run_id, "filed=0 closed=0")

    # Simulate a worker restart: a fresh registry loads the persisted file.
    reg_after_restart = RunRegistry(db_path)
    w = Worker(ctx, run_registry=reg_after_restart)
    delay = w._initial_delay("orphaned-pr-check", 86400)

    # Anchor persisted → next pass waits ~the full interval, not ~1s.
    assert delay > 43200


def test_initial_delay_is_per_repo_scoped(ctx, tmp_path):
    """A recent audit on ONE repo must NOT delay another repo's first run.

    Regression for: audit never ran on robotsix-llmio because _initial_delay
    queried most_recent("audit") with no repo_id, inheriting mill's daily audit
    timestamp — so llmio waited a near-full 24h every restart and never fired.
    With repo scoping, a repo that has never run the agent fires ~immediately.
    """
    import json

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    db_path = tmp_path / "runs.json"
    recent = datetime.now(UTC).isoformat()
    db_path.write_text(
        json.dumps(
            [
                {
                    "id": "a1",
                    "kind": "audit",
                    "repo_id": "robotsix-mill",
                    "started_at": recent,
                    "finished_at": recent,
                    "status": "ok",
                    "summary": "",
                    "error": None,
                }
            ]
        )
    )
    w = Worker(ctx, run_registry=RunRegistry(db_path))
    # mill ran audit just now, but llmio never has → llmio fires soon (base 1.0 + stagger).
    delay_llmio = w._initial_delay("audit", 86400, repo_id="robotsix-llmio")
    assert 1.0 <= delay_llmio <= 420
    # mill itself still sees its own recent run and waits (~interval + stagger).
    delay_mill = w._initial_delay("audit", 86400, repo_id="robotsix-mill")
    assert delay_mill > 86000
    # legacy any-repo call (no repo_id) keeps the old behaviour (~interval + stagger).
    delay_any = w._initial_delay("audit", 86400)
    assert delay_any > 86000


# --- periodic pass root span tests -------------------------------------


async def test_periodic_pass_opens_root_span_before_runner(ctx, monkeypatch):
    """Root span is opened with the correct label before runner_fn is
    invoked, and session_id is passed to runner_fn."""
    import contextlib

    from robotsix_mill.runtime import tracing as tracing_mod

    seen = {}
    captured = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        seen["root_opened"] = True
        seen["session_id"] = sid
        seen["stage"] = name
        yield tracing_mod._NoopRootIO()

    monkeypatch.setattr(tracing_mod, "start_ticket_root_span", fake_root)

    def fake_runner(session_id=None):
        captured["session_id"] = session_id
        captured["root_was_opened"] = seen.get("root_opened", False)
        from robotsix_mill.agents.runners.periodic_runner import PeriodicPassResult

        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id or "",
        )

    sleep_calls = 0
    _real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:  # after initial delay + one loop iteration
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass("test-label", fake_runner, 60)

    assert seen["root_opened"] is True
    assert seen["stage"] == "test-label"
    assert seen["session_id"].startswith("test-label-")
    assert captured["root_was_opened"] is True, (
        "root span must be opened BEFORE runner_fn is invoked"
    )
    assert captured["session_id"] == seen["session_id"]


async def test_periodic_pass_root_span_survives_runner_crash(ctx, monkeypatch):
    """A runner_fn that raises still has its root span opened by the poll
    loop — the Langfuse trace exists even if the runner crashes before
    doing any agent work."""
    import contextlib

    from robotsix_mill.runtime import tracing as tracing_mod

    seen = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        seen["root_opened"] = True
        seen["session_id"] = sid
        yield tracing_mod._NoopRootIO()

    monkeypatch.setattr(tracing_mod, "start_ticket_root_span", fake_root)

    def crashing_runner(session_id=None):
        raise RuntimeError("pre-agent setup failure")

    sleep_calls = 0
    _real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass("crash-test", crashing_runner, 60)

    assert seen["root_opened"] is True, (
        "root span must exist even when runner_fn crashes before the old "
        "start_ticket_root_span would have been reached"
    )


# --- periodic pass per-repo forwards repo_config to start_ticket_root_span ---


async def test_periodic_pass_per_repo_forwards_repo_config_to_span(ctx, monkeypatch):
    """_run_periodic_pass_per_repo must forward repo_config to
    start_ticket_root_span so per-repo Langfuse credentials in
    repos.yaml are used for periodic agent traces."""
    import contextlib

    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime import tracing as tr

    seen: dict = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        seen["root_opened"] = True
        seen["session_id"] = sid
        seen["stage"] = name
        seen["repo_config"] = repo_config
        yield tr._NoopRootIO()

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)

    fake_repo = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="p",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.periodic_passes.get_repos_config",
        lambda: ReposRegistry(repos={"test-repo": fake_repo}),
    )

    captured_repo_config = {}

    def fake_runner(session_id=None, repo_config=None):
        captured_repo_config["value"] = repo_config
        from robotsix_mill.agents.runners.periodic_runner import PeriodicPassResult

        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id or "",
        )

    sleep_calls = 0
    _real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:  # after initial sleep + one body iteration
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass_per_repo(
            "audit",
            fake_runner,
            settings_interval_attr="audit_interval_seconds",
        )

    assert seen.get("root_opened") is True
    assert seen.get("repo_config") is not None, (
        "repo_config must be forwarded to start_ticket_root_span "
        "so per-repo Langfuse credentials are used"
    )
    assert seen["repo_config"] is fake_repo
    assert captured_repo_config.get("value") is fake_repo


async def test_periodic_pass_per_repo_staggers_fan_out(ctx, monkeypatch):
    """A multi-repo fan-out fires due repos staggered across
    ``fan_out_stagger_seconds`` instead of all at once, while a zero
    window fires them back-to-back (no stagger sleep)."""
    import contextlib

    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime import tracing as tr
    from robotsix_mill.runtime.worker import Worker

    fake_repos = {
        f"r{i}": RepoConfig(
            repo_id=f"r{i}",
            board_id=f"b{i}",
            langfuse_project_name="p",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        for i in range(3)
    }
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.periodic_passes.get_repos_config",
        lambda: ReposRegistry(repos=fake_repos),
    )
    # No per-repo YAML override → fall back to Settings (enabled=True).
    monkeypatch.setattr(
        "robotsix_mill.agents.yaml_loader.load_periodic_agent_definition",
        lambda *a, **k: None,
    )

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        yield tr._NoopRootIO()

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)

    ctx.settings.fan_out_stagger_seconds = 30

    fired: list[str] = []

    def fake_runner(session_id=None, repo_config=None):
        fired.append(repo_config.repo_id if repo_config else "")
        from robotsix_mill.agents.runners.periodic_runner import PeriodicPassResult

        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id or "",
        )

    sleep_durations: list[float] = []
    _real_sleep = asyncio.sleep
    tick = 0

    async def counting_sleep(delay: float) -> None:
        nonlocal tick
        tick += 1
        sleep_durations.append(delay)
        # Cancel after first tick + 2 stagger sleeps + the following tick.
        if tick >= 4:
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass_per_repo(
            "audit",
            fake_runner,
            settings_interval_attr="audit_interval_seconds",
        )

    assert fired == ["r0", "r1", "r2"], f"all three due repos must fire, got {fired}"
    # sleep[0] = first-tick settling delay; sleeps[1:3] are the two
    # inter-repo stagger delays of 30 / 3 = 10 s each.
    assert sleep_durations[1:3] == [10.0, 10.0], (
        f"expected staggered [10.0, 10.0] sleeps, got {sleep_durations[1:3]}"
    )


async def test_periodic_pass_per_repo_fan_out_zero_stagger_no_sleep(ctx, monkeypatch):
    """With fan_out_stagger_seconds=0 a multi-repo fan-out does not add
    inter-repo sleeps (manual/normal flow unchanged)."""
    import contextlib

    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime import tracing as tr
    from robotsix_mill.runtime.worker import Worker

    fake_repos = {
        f"r{i}": RepoConfig(
            repo_id=f"r{i}",
            board_id=f"b{i}",
            langfuse_project_name="p",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        for i in range(3)
    }
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.periodic_passes.get_repos_config",
        lambda: ReposRegistry(repos=fake_repos),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.yaml_loader.load_periodic_agent_definition",
        lambda *a, **k: None,
    )

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        yield tr._NoopRootIO()

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)

    ctx.settings.fan_out_stagger_seconds = 0

    fired: list[str] = []

    def fake_runner(session_id=None, repo_config=None):
        fired.append(repo_config.repo_id if repo_config else "")
        from robotsix_mill.agents.runners.periodic_runner import PeriodicPassResult

        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id or "",
        )

    sleep_durations: list[float] = []
    _real_sleep = asyncio.sleep
    tick = 0

    async def counting_sleep(delay: float) -> None:
        nonlocal tick
        tick += 1
        sleep_durations.append(delay)
        if tick >= 2:  # first tick + next tick → cancel
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass_per_repo(
            "audit",
            fake_runner,
            settings_interval_attr="audit_interval_seconds",
        )

    assert fired == ["r0", "r1", "r2"], f"all three due repos must fire, got {fired}"
    # Only the first-tick settling delay and the next-tick poll sleep
    # were recorded — no stagger sleeps in between.
    assert len(sleep_durations) == 2, (
        f"expected no stagger sleeps, got {sleep_durations}"
    )


# ---------------------------------------------------------------------------
# Graceful shutdown — periodic passes finish before stop()
# ---------------------------------------------------------------------------


async def test_stop_awaits_inflight_passes(ctx, monkeypatch):
    """stop() must wait for in-flight ``_tracked_to_thread`` calls to
    finish, not cancel them. Without this a SIGTERM during a survey
    run kills the agent mid-pass."""
    import threading
    import time

    w = Worker(ctx, run_registry=None)
    started = threading.Event()
    finished_at: dict = {}

    def slow_runner():
        # Simulates a survey pass that needs to flush memory + db
        # before exit. Should not be killed.
        started.set()
        time.sleep(0.2)
        finished_at["t"] = time.monotonic()
        return "ok"

    # Spawn the tracked thread call exactly as a periodic loop would.
    call_task = asyncio.create_task(w._tracked_to_thread(slow_runner))
    # Give the thread a moment to actually enter slow_runner.
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.005)
    assert started.is_set(), "slow_runner never started"

    # Now simulate teardown. stop() should await the in-flight thread.
    stop_started = time.monotonic()
    await w.stop()
    stop_elapsed = time.monotonic() - stop_started

    assert "t" in finished_at, (
        "slow_runner did not complete before stop() returned — "
        "the in-flight pass was killed prematurely"
    )
    # The wait should have taken at least most of the 0.2 s nap.
    assert stop_elapsed >= 0.15
    # Cleanup the call task (it should be done).
    assert call_task.done()


async def test_stop_grace_timeout_does_not_hang(ctx, monkeypatch):
    """If a pass runs past ``shutdown_grace_seconds`` stop() logs a
    warning and proceeds — it must never hang the process."""
    import threading
    import time

    # Tight grace so the test is fast.
    monkeypatch.setattr(ctx.settings, "shutdown_grace_seconds", 1)
    w = Worker(ctx, run_registry=None)
    started = threading.Event()

    def very_slow_runner():
        started.set()
        time.sleep(5)  # > grace
        return "late"

    call_task = asyncio.create_task(w._tracked_to_thread(very_slow_runner))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.005)

    t0 = time.monotonic()
    await w.stop()
    elapsed = time.monotonic() - t0
    # Stop returned somewhere around the 1 s grace, not 5 s.
    assert elapsed < 3.0, (
        f"stop() waited {elapsed:.1f}s; should have bailed out near the 1s grace"
    )
    # The shielded call is still running in the background. Cancel
    # the wrapper task so we don't leak it past the test.
    call_task.cancel()


def test_periodic_pass_uses_per_repo_registry(ctx, tmp_path):
    """Periodic passes record into — and read cadence from — the repo's OWN
    registry, so a run shows in that repo's /runs list (not the lead repo's).
    Regression: audit ran for robotsix-llmio but landed in mill's runs.json, so
    the per-repo /runs API (which reads <board>/runs.json) showed 0 audit runs.
    """

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    mill_reg = RunRegistry(tmp_path / "mill.json")
    llmio_reg = RunRegistry(tmp_path / "llmio.json")
    w = Worker(
        ctx,
        mill_reg,
        run_registries={
            "robotsix-mill": mill_reg,
            "robotsix-llmio": llmio_reg,
        },
    )
    rc_llmio = SimpleNamespace(board_id="robotsix-llmio", repo_id="robotsix-llmio")

    # _registry_for routes by board_id; unknown board / None → default.
    assert w._registry_for(rc_llmio) is llmio_reg
    assert w._registry_for(None) is mill_reg
    assert w._registry_for(SimpleNamespace(board_id="x", repo_id="x")) is mill_reg

    # A run recorded for llmio lands in llmio's registry, not mill's.
    rid = llmio_reg.start("audit", repo_id="robotsix-llmio")
    llmio_reg.finish_ok(rid, "ok")
    assert llmio_reg.most_recent("audit", repo_id="robotsix-llmio") is not None
    assert mill_reg.most_recent("audit", repo_id="robotsix-llmio") is None

    # Cadence reads the per-repo store: llmio's own fresh run → "recent".
    delay = w._initial_delay(
        "audit", 86400, repo_id="robotsix-llmio", registry=w._registry_for(rc_llmio)
    )
    assert delay > 86000


async def test_meta_pass_loop_records_into_meta_registry(ctx, tmp_path, monkeypatch):
    """The meta pass records into the dedicated meta registry tagged
    repo_id="meta", not the default/lead-repo registry — so meta runs
    show on the meta board's runs drawer rather than the main board."""

    import robotsix_mill.meta.runner as meta_runner
    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    mill_reg = RunRegistry(tmp_path / "mill.json")
    meta_reg = RunRegistry(tmp_path / "meta.json")
    w = Worker(
        ctx,
        mill_reg,
        run_registries={"robotsix-mill": mill_reg, Worker._META_BOARD: meta_reg},
    )

    def _fake_run_meta_pass(*, session_id):
        return SimpleNamespace(
            extraction_drafts_created=[{"id": "x1"}],
            alignment_drafts_created=[],
        )

    monkeypatch.setattr(meta_runner, "run_meta_pass", _fake_run_meta_pass)
    monkeypatch.setattr(w, "_initial_delay", lambda *a, **kw: 0.0)

    task = asyncio.create_task(w._meta_pass_loop())
    try:
        for _ in range(500):
            entries = meta_reg.list_all()
            if entries and entries[0]["status"] == "ok":
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    entries = meta_reg.list_all()
    assert len(entries) == 1
    assert entries[0]["kind"] == "meta"
    assert entries[0]["repo_id"] == "meta"
    assert entries[0]["status"] == "ok"
    # Nothing leaked into the default/lead-repo registry.
    assert mill_reg.list_all() == []
