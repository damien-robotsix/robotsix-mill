"""Tests for the target-branch CI monitor poll loop."""

import json
import time
from datetime import datetime, timedelta, timezone


from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
    target_branch_for,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import Comment, SourceKind, Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import Worker
from robotsix_mill.stages import StageContext
from robotsix_mill.agents.ci_fixing import CiFixResult
from robotsix_mill.vcs.git_ops import PostPushResult


def _ctx(tmp_path, repo_config=None, **env):
    """Build a StageContext for CI monitor tests.

    *repo_config* controls the per-repo CI monitor settings
    (ci_monitor_enabled, ci_monitor_interval_seconds).  When
    omitted, the default repo has ci_monitor_enabled=True and
    ci_monitor_interval_seconds=1.

    The repos registry is monkeypatched so the poll loop picks up
    the test repo.
    """
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    env.setdefault("require_approval", "false")
    s = Settings(**env)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.get("FORGE_TOKEN")
    if ft is not None:
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(forge_token=ft)
    db.init_db(s, board_id="test-board")

    if repo_config is None:
        repo_config = RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            ci_monitor_enabled=True,
            ci_monitor_interval_seconds=60,
        )
    # Patch the repos registry so the poll loop sees our test repo.
    _reset_repos_config()
    import robotsix_mill.config as _cfg

    _cfg._repos_config = ReposRegistry(repos={repo_config.repo_id: repo_config})

    return StageContext(
        settings=s,
        service=TicketService(s, board_id=repo_config.board_id),
        repo_config=repo_config,
    )


def _make_fake_forge(monkeypatch, runs=None, logs="", raise_on_logs=False):
    class FakeForge:
        """Controllable fake forge for CI monitor tests."""

        def __init__(self, runs=None, logs="", raise_on_logs=False):
            self.runs = runs or []
            self.logs = logs
            self.raise_on_logs = raise_on_logs
            self.logs_call_count = 0

        def list_workflow_runs(self, *, branch=None, head_sha=None):
            return self.runs

        def fetch_workflow_job_logs(self, *, run_id):
            self.logs_call_count += 1
            if self.raise_on_logs:
                raise ConnectionError("simulated ConnectError")
            return self.logs

    forge = FakeForge(runs=runs, logs=logs, raise_on_logs=raise_on_logs)

    def _fake_get_forge(settings, repo_config=None):
        return forge

    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        _fake_get_forge,
    )
    return forge


# ---------------------------------------------------------------------------


def test_detects_new_failure_and_creates_draft(tmp_path, monkeypatch):
    """One failing run not in dedup state → service.create called with source='ci'."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "docker-publish",
                "workflow_id": 200,
                "head_sha": "abc",
                "conclusion": "failure",
                "html_url": "http://run/1",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
        logs="build error\n",
    )

    # Clear any existing state file.
    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    # Run ONE poll cycle by scheduling the task then cancelling it.
    worker = Worker(ctx)
    worker._ci_monitor_task = None  # force fresh task
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)
    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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
    assert t.priority is True

    # Verify state file was written.
    assert state_path.exists()
    state = json.loads(state_path.read_text("utf-8"))
    assert "200:abc" in state["seen"]


def test_dedup_skips_already_seen_failure(tmp_path, monkeypatch):
    """State file already has (workflow_id, head_sha) → no draft created."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "CI",
                "workflow_id": 100,
                "head_sha": "abc",
                "conclusion": "failure",
                "html_url": "http://x",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
    )

    # Pre-populate the dedup state with the same key.
    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"seen": {"100:abc": time.time()}}), "utf-8")

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 2,
                "name": "CI",
                "workflow_id": 100,
                "head_sha": "def",
                "conclusion": "failure",
                "html_url": "http://x",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
    )

    # State has 100:abc but the run is 100:def.
    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"seen": {"100:abc": time.time()}}), "utf-8")

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(monkeypatch, runs=[])

    # Pre-populate with one old entry and one recent entry.
    now = int(time.time())
    old = now - (31 * 86400)  # 31 days ago
    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "seen": {"100:old": old, "200:recent": now},
            }
        ),
        "utf-8",
    )

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "CI",
                "workflow_id": 100,
                "head_sha": "abc",
                "conclusion": "success",
                "html_url": "http://x",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
    )

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "CI",
                "workflow_id": 100,
                "head_sha": "abc",
                "conclusion": None,
                "html_url": "http://x",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
    )

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "docker-publish",
                "workflow_id": 200,
                "head_sha": "abc",
                "conclusion": "failure",
                "html_url": "http://run/1",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
        logs="Step 5/10: ERROR: build failed\n",
    )

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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


def test_monitor_skips_when_disabled_per_repo(tmp_path, monkeypatch):
    """When no repo has ci_monitor_enabled=True, the poll task is not created."""
    ctx = _ctx(
        tmp_path,
        repo_config=RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            ci_monitor_enabled=False,
            ci_monitor_interval_seconds=86400,
        ),
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    # CI monitor is not enabled for any repo — start() should not
    # create a _ci_monitor_task.
    worker = Worker(ctx)
    worker._ci_monitor_task = None  # ensure clean state
    # Call start() inside an asyncio loop to exercise the startup gate.
    import asyncio

    async def _check():
        worker.start()
        assert worker._ci_monitor_task is None
        await worker.stop()

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_check())
    loop.close()


def test_existing_pr_ci_fix_path_still_works(tmp_path, monkeypatch):
    """The existing test_fix_success_push_success_returns_implement_complete still
    passes after the refactor — i.e., the ci_fix stage still works
    when log fetching fails (the exception path is handled)."""
    from robotsix_mill.forge import github as gh_mod
    from robotsix_mill.stages.ci_fix import CIFixStage as CFS

    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    # check_status returns failure.
    monkeypatch.setattr(
        gh_mod.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    # pr_status returns a sha.
    monkeypatch.setattr(
        gh_mod.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "http://pr",
            "mergeable": True,
            "sha": "abc123",
        },
    )
    # list_workflow_runs raises (simulating no runs or API issue).
    monkeypatch.setattr(
        gh_mod.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: (_ for _ in ()).throw(
            RuntimeError("not available")
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    push_seen = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        push_seen.update(branch=branch, token=token)
        return PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        fake_post_check,
    )

    # Create a FIXING_CI ticket via helper.
    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.FIXING_CI,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = CFS().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert push_seen["branch"] == f"mill/{t.id}"


# ---------------------------------------------------------------------------
# Composite (workflow, branch) consolidation dedup.
# ---------------------------------------------------------------------------


def _run_one_cycle(worker, monkeypatch):
    """Drive exactly one CI monitor poll cycle then cancel the loop."""
    import asyncio

    loop = asyncio.new_event_loop()

    async def _run():
        async def _fast_sleep(s):
            if s >= 1:
                raise asyncio.CancelledError()

        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        try:
            await worker._ci_monitor_poll_loop()
        except asyncio.CancelledError:
            pass

    loop.run_until_complete(_run())
    loop.close()


def _set_ticket_created_at(ctx, ticket_id, when):
    with db.session(ctx.settings, "test-board") as s:
        t = s.get(Ticket, ticket_id)
        t.created_at = when
        s.add(t)
        s.commit()


def _seed_comment(ctx, ticket_id, body, when):
    with db.session(ctx.settings, "test-board") as s:
        c = Comment(ticket_id=ticket_id, body=body, author="user")
        c.created_at = when
        s.add(c)
        s.commit()


def _make_canonical_ci_ticket(ctx, wf_name, target, title, created_minutes_ago):
    """Create a non-terminal source=ci ticket carrying the body markers but
    with a renamed title, aged *created_minutes_ago* in the past."""
    body = (
        f"**Workflow:** {wf_name}\n"
        f"**Branch:** {target}\n"
        f"**Run:** [1](http://run/1)\n"
        f"**Commit:** `old-sha`\n"
    )
    t = ctx.service.create(title=title, description=body, source=SourceKind.CI)
    when = datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
    _set_ticket_created_at(ctx, t.id, when)
    return t


def test_consolidates_recurrence_into_renamed_canonical(tmp_path, monkeypatch):
    """A new-commit recurrence folds into a renamed canonical via a comment."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    target = target_branch_for(ctx.settings, ctx.repo_config)
    canonical = _make_canonical_ci_ticket(
        ctx,
        wf_name="Docs",
        target=target,
        title="Root-cause recurring Docs failures",
        created_minutes_ago=10,
    )

    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 99,
                "name": "Docs",
                "workflow_id": 300,
                "head_sha": "newsha",
                "conclusion": "failure",
                "html_url": "http://run/99",
                "created_at": "2026-06-12T09:00:00Z",
            },
        ],
        logs="boom\n",
    )

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)
    _run_one_cycle(worker, monkeypatch)

    # No new CI ticket — only the canonical remains.
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1
    assert ci_tickets[0].id == canonical.id

    # A consolidation comment referencing the new run/commit was added.
    comments = ctx.service.list_comments(canonical.id)
    assert any("99" in c.body and "newsha" in c.body for c in comments)

    # The new commit key is recorded in seen.
    state = json.loads(state_path.read_text("utf-8"))
    assert "300:newsha" in state["seen"]


def test_aged_canonical_still_consolidates(tmp_path, monkeypatch):
    """A canonical whose last activity is 60+ min old still consolidates the
    recurrence — there is no freshness window, only the terminal-state
    boundary."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    target = target_branch_for(ctx.settings, ctx.repo_config)
    canonical = _make_canonical_ci_ticket(
        ctx,
        wf_name="Docs",
        target=target,
        title="Root-cause recurring Docs failures",
        created_minutes_ago=60,  # aged well past the old 40-min window
    )

    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 99,
                "name": "Docs",
                "workflow_id": 300,
                "head_sha": "newsha",
                "conclusion": "failure",
                "html_url": "http://run/99",
                "created_at": "2026-06-12T09:00:00Z",
            },
        ],
        logs="boom\n",
    )

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)
    _run_one_cycle(worker, monkeypatch)

    # No new CI ticket — the aged canonical absorbed the recurrence.
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1
    assert ci_tickets[0].id == canonical.id
    assert not any(t.title == f"CI failure: Docs on {target}" for t in ci_tickets)

    # A consolidation comment referencing the new run/commit was added.
    comments = ctx.service.list_comments(canonical.id)
    assert any("99" in c.body and "newsha" in c.body for c in comments)


def test_comment_refreshes_window_across_recurrences(tmp_path, monkeypatch):
    """Two sequential recurrences both consolidate into the one canonical.

    Consolidation no longer depends on any freshness window — the aged
    canonical absorbs each new-commit recurrence regardless of activity age.
    """
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    target = target_branch_for(ctx.settings, ctx.repo_config)
    canonical = _make_canonical_ci_ticket(
        ctx,
        wf_name="Docs",
        target=target,
        title="Root-cause recurring Docs failures",
        created_minutes_ago=60,
    )

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    forge = _make_fake_forge(monkeypatch, logs="boom\n")
    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    # Cycle 1: recurrence on commit shaA.
    forge.runs = [
        {
            "id": 50,
            "name": "Docs",
            "workflow_id": 300,
            "head_sha": "shaA",
            "conclusion": "failure",
            "html_url": "http://run/50",
            "created_at": "2026-06-12T08:37:00Z",
        },
    ]
    _run_one_cycle(worker, monkeypatch)

    # Cycle 2: recurrence on a new commit shaB — relies on cycle 1's comment
    # (now) keeping the window fresh.
    forge.runs = [
        {
            "id": 51,
            "name": "Docs",
            "workflow_id": 300,
            "head_sha": "shaB",
            "conclusion": "failure",
            "html_url": "http://run/51",
            "created_at": "2026-06-12T09:00:00Z",
        },
    ]
    _run_one_cycle(worker, monkeypatch)

    # Still only the canonical CI ticket — zero duplicates filed.
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1
    assert ci_tickets[0].id == canonical.id

    comments = ctx.service.list_comments(canonical.id)
    assert any("50" in c.body and "shaA" in c.body for c in comments)
    assert any("51" in c.body and "shaB" in c.body for c in comments)


# === content-based fingerprint dedup =====================================


def test_content_dedup_same_fingerprint_consolidates(tmp_path, monkeypatch):
    """Cycle 1 creates a draft with a ci_fp label; cycle 2 with the same
    fingerprint but different workflow → label match consolidates."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(monkeypatch, logs="error: division by zero\n")

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    # Cycle 1: workflow "Build"
    forge.runs = [
        {
            "id": 1,
            "name": "Build",
            "workflow_id": 100,
            "head_sha": "abc",
            "conclusion": "failure",
            "html_url": "http://run/1",
            "created_at": "2025-01-01T00:00:00Z",
        },
    ]

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)
    _run_one_cycle(worker, monkeypatch)

    # Verify draft created with ci_fp label.
    tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(tickets) == 1
    labels = json.loads(tickets[0].labels or "[]")
    assert any(label.startswith("ci_fp:") for label in labels)

    # Cycle 2: different workflow ("Test") but same log content →
    # same fingerprint → label match consolidates instead of new ticket.
    forge.runs = [
        {
            "id": 2,
            "name": "Test",
            "workflow_id": 200,
            "head_sha": "def",
            "conclusion": "failure",
            "html_url": "http://run/2",
            "created_at": "2025-01-01T00:00:00Z",
        },
    ]
    _run_one_cycle(worker, monkeypatch)

    # No second ticket created.
    tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(tickets) == 1

    # Consolidation comment references the new run/commit.
    comments = ctx.service.list_comments(tickets[0].id)
    assert any("2" in c.body and "def" in c.body for c in comments)


def test_content_dedup_different_fingerprint_new_ticket(tmp_path, monkeypatch):
    """Cycle 1 creates a draft with fingerprint A; cycle 2 with genuinely
    different error content (fingerprint B) creates a second ticket."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(monkeypatch, logs="error: division by zero\n")

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    # Cycle 1: workflow "Build", fingerprint A.
    forge.runs = [
        {
            "id": 1,
            "name": "Build",
            "workflow_id": 100,
            "head_sha": "abc",
            "conclusion": "failure",
            "html_url": "http://run/1",
            "created_at": "2025-01-01T00:00:00Z",
        },
    ]

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)
    _run_one_cycle(worker, monkeypatch)

    tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(tickets) == 1

    # Cycle 2: different workflow, different log content → fingerprint B.
    forge.logs = "error: index out of bounds in module X\n"
    forge.runs = [
        {
            "id": 2,
            "name": "Test",
            "workflow_id": 200,
            "head_sha": "def",
            "conclusion": "failure",
            "html_url": "http://run/2",
            "created_at": "2025-01-01T00:00:00Z",
        },
    ]
    _run_one_cycle(worker, monkeypatch)

    # Second ticket created — fingerprints differ.
    tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(tickets) == 2

    # Labels differ between the two tickets.
    all_labels = [json.loads(t.labels or "[]") for t in tickets]
    fp_labels = [
        [label for label in ls if label.startswith("ci_fp:")] for ls in all_labels
    ]
    assert all(len(fp) == 1 for fp in fp_labels)
    assert fp_labels[0] != fp_labels[1]


def _run_ci_poll_cycle(worker, ctx, monkeypatch):
    """Drive a single ``_poll_one_repo_ci`` call deterministically."""
    import asyncio
    import re

    settings = ctx.settings
    rc = ctx.repo_config
    target = target_branch_for(settings, rc)
    ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    async def _noop_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            worker._poll_one_repo_ci(rc, target, time.time(), 30 * 86400, ansi_re)
        )
    finally:
        loop.close()


def test_log_fetch_error_defers_then_files_with_error_note(tmp_path, monkeypatch):
    """A fetch that errors every attempt defers across poll cycles instead of
    filing an empty draft, then files with the error surfaced once the
    deferral budget is exhausted."""
    from robotsix_mill.runtime.worker import poll_loops

    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 7,
                "name": "Docs",
                "workflow_id": 300,
                "head_sha": "deadbeef",
                "conclusion": "failure",
                "html_url": "http://run/7",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
        raise_on_logs=True,
    )

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    # Cycles 1..MAX defer: no ticket, deferral bookkeeping persisted.
    for cycle in range(1, poll_loops._CI_LOG_FETCH_MAX_DEFERRALS + 1):
        _run_ci_poll_cycle(worker, ctx, monkeypatch)
        assert [t for t in ctx.service.list() if t.source == "ci"] == []
        state = json.loads(state_path.read_text("utf-8"))
        assert state["deferred"]["300:deadbeef"]["n"] == cycle
        assert "300:deadbeef" not in state.get("seen", {})

    # Each cycle retried the fetch _CI_LOG_FETCH_ATTEMPTS times.
    assert (
        forge.logs_call_count
        == poll_loops._CI_LOG_FETCH_ATTEMPTS * poll_loops._CI_LOG_FETCH_MAX_DEFERRALS
    )

    # Next cycle exhausts the budget → files the draft with the error note.
    _run_ci_poll_cycle(worker, ctx, monkeypatch)
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1
    body = ctx.service.workspace(ci_tickets[0]).read_description() or ""
    assert "Could not fetch the run logs" in body
    assert "ConnectError" in body or "ConnectionError" in body
    assert "http://run/7" in body

    # Marked seen and deferral record cleared.
    state = json.loads(state_path.read_text("utf-8"))
    assert "300:deadbeef" in state["seen"]
    assert "300:deadbeef" not in state.get("deferred", {})


# === skip_ci toggle =======================================================


def test_ci_monitor_skips_repo_when_skip_ci_true(tmp_path, monkeypatch):
    """When a repo has skip_ci=True in its .robotsix-mill/config.yaml,
    the CI monitor skips it entirely — no poll, no tickets filed."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    forge = _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "docker-publish",
                "workflow_id": 200,
                "head_sha": "abc",
                "conclusion": "failure",
                "html_url": "http://run/1",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
        logs="build error\n",
    )

    # Write skip_ci: true into the repo's config via the piggyback clone dir.
    clone_dir = ctx.settings.data_dir / "test-repo" / "periodic_workspace" / "repo"
    clone_dir.mkdir(parents=True, exist_ok=True)
    (clone_dir / ".git").mkdir(exist_ok=True)
    cfg_dir = clone_dir / ".robotsix-mill"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text("skip_ci: true\n", encoding="utf-8")

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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

    # No CI tickets should have been filed.
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 0

    # The forge should never have been called.
    assert forge.logs_call_count == 0


def test_ci_monitor_polls_repo_when_skip_ci_false(tmp_path, monkeypatch):
    """When a repo has skip_ci=False, the CI monitor polls it normally."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "docker-publish",
                "workflow_id": 200,
                "head_sha": "abc",
                "conclusion": "failure",
                "html_url": "http://run/1",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
        logs="build error\n",
    )

    # Write skip_ci: false into the repo's config.
    clone_dir = ctx.settings.data_dir / "test-repo" / "periodic_workspace" / "repo"
    clone_dir.mkdir(parents=True, exist_ok=True)
    (clone_dir / ".git").mkdir(exist_ok=True)
    cfg_dir = clone_dir / ".robotsix-mill"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text("skip_ci: false\n", encoding="utf-8")

    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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

    # A CI ticket should have been filed (normal behaviour).
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1


def test_ci_monitor_polls_repo_when_no_config_clone(tmp_path, monkeypatch):
    """When no piggyback clone exists (_find_config_clone_dir returns None),
    load_repo_skip_ci returns False and the repo is polled normally."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    _make_fake_forge(
        monkeypatch,
        runs=[
            {
                "id": 1,
                "name": "docker-publish",
                "workflow_id": 200,
                "head_sha": "abc",
                "conclusion": "failure",
                "html_url": "http://run/1",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
        logs="build error\n",
    )

    # Ensure NO clone dirs exist at all.
    state_path = ctx.settings.data_dir / "test-repo" / "ci_monitor_state.json"
    if state_path.exists():
        state_path.unlink()

    worker = Worker(ctx)
    worker._ci_monitor_task = None
    monkeypatch.setattr(worker, "_initial_delay", lambda kind, interval: 0.0)

    import asyncio

    loop = asyncio.new_event_loop()

    async def _run_one_cycle():
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

    # A CI ticket should have been filed (normal behaviour — no clone = no skip).
    ci_tickets = [t for t in ctx.service.list() if t.source == "ci"]
    assert len(ci_tickets) == 1
