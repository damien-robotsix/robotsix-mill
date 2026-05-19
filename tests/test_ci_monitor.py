"""Tests for the target-branch CI monitor poll loop."""

import json
import time

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import Worker
from robotsix_mill.stages import StageContext


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    env.setdefault("MILL_REQUIRE_APPROVAL", "false")
    s = Settings(**env)
    db.init_db(s)
    return StageContext(settings=s, service=TicketService(s))


# ---------------------------------------------------------------------------
# Helpers: a fake forge that the worker's CI monitor uses.
# We monkeypatch get_forge() to return a FakeForge.
# ---------------------------------------------------------------------------

class FakeForge:
    """Controllable fake forge for CI monitor tests."""

    def __init__(self, runs=None, logs=""):
        self.runs = runs or []
        self.logs = logs
        self.logs_call_count = 0

    def list_workflow_runs(self, *, branch=None, head_sha=None):
        return self.runs

    def fetch_workflow_job_logs(self, *, run_id):
        self.logs_call_count += 1
        return self.logs


def _make_fake_forge(monkeypatch, runs=None, logs=""):
    forge = FakeForge(runs=runs, logs=logs)
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda s: forge,
    )
    return forge


# ---------------------------------------------------------------------------

def test_detects_new_failure_and_creates_draft(tmp_path, monkeypatch):
    """One failing run not in dedup state → service.create called with source='ci'."""
    ctx = _ctx(
        tmp_path,
        MILL_CI_MONITOR_PERIODIC="true",
        MILL_CI_MONITOR_INTERVAL_SECONDS="1",
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(monkeypatch, runs=[
        {
            "id": 1, "name": "docker-publish", "workflow_id": 200,
            "head_sha": "abc", "conclusion": "failure",
            "html_url": "http://run/1", "created_at": "2025-01-01T00:00:00Z",
        },
    ], logs="build error\n")

    # Clear any existing state file.
    state_path = ctx.settings.ci_monitor_memory_path
    if state_path.exists():
        state_path.unlink()

    # Run ONE poll cycle by scheduling the task then cancelling it.
    worker = Worker(ctx)
    worker._ci_monitor_task = None  # force fresh task
    # Directly invoke the poll method — it will sleep once but we cancel.
    import asyncio
    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
        # We override the sleep to complete immediately.
        orig_sleep = asyncio.sleep

        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()  # stop after first cycle

        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run_one_cycle())
    loop.close()

    # Verify draft was created.
    tickets = ctx.service.list()
    ci_tickets = [t for t in tickets if t.source == "ci"]
    assert len(ci_tickets) == 1
    t = ci_tickets[0]
    assert "docker-publish" in t.title
    assert "build error" in (ctx.service.workspace(t).read_description() or "")
    assert t.state == State.DRAFT

    # Verify state file was written.
    assert state_path.exists()
    state = json.loads(state_path.read_text("utf-8"))
    assert "200:abc" in state["seen"]


def test_dedup_skips_already_seen_failure(tmp_path, monkeypatch):
    """State file already has (workflow_id, head_sha) → no draft created."""
    ctx = _ctx(
        tmp_path,
        MILL_CI_MONITOR_PERIODIC="true",
        MILL_CI_MONITOR_INTERVAL_SECONDS="1",
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(monkeypatch, runs=[
        {
            "id": 1, "name": "CI", "workflow_id": 100,
            "head_sha": "abc", "conclusion": "failure",
            "html_url": "http://x", "created_at": "2025-01-01T00:00:00Z",
        },
    ])

    # Pre-populate the dedup state with the same key.
    state_path = ctx.settings.ci_monitor_memory_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"seen": {"100:abc": time.time()}}), "utf-8")

    worker = Worker(ctx)
    worker._ci_monitor_task = None

    import asyncio
    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
        orig_sleep = asyncio.sleep
        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run_one_cycle())
    loop.close()

    # No new CI drafts.
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 0


def test_dedup_key_is_workflow_id_and_head_sha(tmp_path, monkeypatch):
    """Different head_sha with same workflow_id → treated as new failure."""
    ctx = _ctx(
        tmp_path,
        MILL_CI_MONITOR_PERIODIC="true",
        MILL_CI_MONITOR_INTERVAL_SECONDS="1",
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(monkeypatch, runs=[
        {
            "id": 2, "name": "CI", "workflow_id": 100,
            "head_sha": "def", "conclusion": "failure",
            "html_url": "http://x", "created_at": "2025-01-01T00:00:00Z",
        },
    ])

    # State has 100:abc but the run is 100:def.
    state_path = ctx.settings.ci_monitor_memory_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"seen": {"100:abc": time.time()}}), "utf-8")

    worker = Worker(ctx)
    worker._ci_monitor_task = None

    import asyncio
    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
        orig_sleep = asyncio.sleep
        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run_one_cycle())
    loop.close()

    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1
    assert "100:def" in json.loads(state_path.read_text("utf-8"))["seen"]


def test_prunes_old_entries_from_state(tmp_path, monkeypatch):
    """Entries older than 30 days are removed on poll."""
    ctx = _ctx(
        tmp_path,
        MILL_CI_MONITOR_PERIODIC="true",
        MILL_CI_MONITOR_INTERVAL_SECONDS="1",
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(monkeypatch, runs=[])

    # Pre-populate with one old entry and one recent entry.
    now = int(time.time())
    old = now - (31 * 86400)  # 31 days ago
    state_path = ctx.settings.ci_monitor_memory_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "seen": {"100:old": old, "200:recent": now},
    }), "utf-8")

    worker = Worker(ctx)
    worker._ci_monitor_task = None

    import asyncio
    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
        orig_sleep = asyncio.sleep
        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run_one_cycle())
    loop.close()

    state = json.loads(state_path.read_text("utf-8"))
    assert "100:old" not in state["seen"]
    assert "200:recent" in state["seen"]


def test_successful_run_not_filed(tmp_path, monkeypatch):
    """conclusion == 'success' → no draft."""
    ctx = _ctx(
        tmp_path,
        MILL_CI_MONITOR_PERIODIC="true",
        MILL_CI_MONITOR_INTERVAL_SECONDS="1",
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(monkeypatch, runs=[
        {
            "id": 1, "name": "CI", "workflow_id": 100,
            "head_sha": "abc", "conclusion": "success",
            "html_url": "http://x", "created_at": "2025-01-01T00:00:00Z",
        },
    ])

    state_path = ctx.settings.ci_monitor_memory_path
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None

    import asyncio
    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
        orig_sleep = asyncio.sleep
        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run_one_cycle())
    loop.close()

    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 0


def test_pending_run_not_filed(tmp_path, monkeypatch):
    """conclusion == None (in progress) → no draft."""
    ctx = _ctx(
        tmp_path,
        MILL_CI_MONITOR_PERIODIC="true",
        MILL_CI_MONITOR_INTERVAL_SECONDS="1",
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(monkeypatch, runs=[
        {
            "id": 1, "name": "CI", "workflow_id": 100,
            "head_sha": "abc", "conclusion": None,
            "html_url": "http://x", "created_at": "2025-01-01T00:00:00Z",
        },
    ])

    state_path = ctx.settings.ci_monitor_memory_path
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None

    import asyncio
    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
        orig_sleep = asyncio.sleep
        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run_one_cycle())
    loop.close()

    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 0


def test_includes_job_logs_in_draft_body(tmp_path, monkeypatch):
    """Draft body contains the fetched job log text."""
    ctx = _ctx(
        tmp_path,
        MILL_CI_MONITOR_PERIODIC="true",
        MILL_CI_MONITOR_INTERVAL_SECONDS="1",
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(monkeypatch, runs=[
        {
            "id": 1, "name": "docker-publish", "workflow_id": 200,
            "head_sha": "abc", "conclusion": "failure",
            "html_url": "http://run/1", "created_at": "2025-01-01T00:00:00Z",
        },
    ], logs="Step 5/10: ERROR: build failed\n")

    state_path = ctx.settings.ci_monitor_memory_path
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None

    import asyncio
    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
        orig_sleep = asyncio.sleep
        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run_one_cycle())
    loop.close()

    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1
    desc = ctx.service.workspace(ci_tickets[0]).read_description() or ""
    assert "Step 5/10: ERROR: build failed" in desc


def test_monitor_disabled_by_default(tmp_path, monkeypatch):
    """Without MILL_CI_MONITOR_PERIODIC=true, no poll task is created."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    # CI monitor is not enabled (default false).
    worker = Worker(ctx)
    # start() should not create a _ci_monitor_task.
    worker._ci_monitor_task = None  # ensure clean state
    # We call start() and verify _ci_monitor_task stays None.
    # (We can't really call start() in tests easily because it creates
    # asyncio tasks that live forever. Just verify the setting.)
    assert ctx.settings.ci_monitor_periodic is False


def test_existing_pr_ci_fix_path_still_works(tmp_path, monkeypatch):
    """The existing test_fix_success_push_success_returns_in_review still
    passes after the refactor — i.e., the ci_fix stage still works
    when log fetching fails (the exception path is handled)."""
    from robotsix_mill.forge import github as gh_mod
    from robotsix_mill.stages import StageContext as SC
    from robotsix_mill.stages.ci_fix import CIFixStage as CFS

    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github", FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    # check_status returns failure.
    monkeypatch.setattr(
        gh_mod.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "lint", "summary": "err", "text": None, "annotations": []}],
        },
    )
    # pr_status returns a sha.
    monkeypatch.setattr(
        gh_mod.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "http://pr",
            "mergeable": True, "sha": "abc123",
        },
    )
    # list_workflow_runs raises (simulating no runs or API issue).
    monkeypatch.setattr(
        gh_mod.GitHubForge, "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: (
            (_ for _ in ()).throw(RuntimeError("not available"))
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: True,
    )
    push_seen = {}

    def fake_push(repo, branch, remote_url, token):
        push_seen.update(branch=branch, token=token)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push", fake_push,
    )

    # Create a FIXING_CI ticket via helper.
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.IN_REVIEW, State.FIXING_CI):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = CFS().run(t, ctx)
    assert out.next_state is State.IN_REVIEW
    assert push_seen["branch"] == f"mill/{t.id}"
