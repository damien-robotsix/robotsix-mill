"""Tests for the periodic stale-branch cleanup pass."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from robotsix_mill.forge.base import BranchInfo
from robotsix_mill.runtime.worker import _branch_is_stale


# ---------------------------------------------------------------------------
# _branch_is_stale unit tests — every guard
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
OLD_DATE = FIXED_NOW - timedelta(days=60)  # well past 30-day cutoff
RECENT_DATE = FIXED_NOW - timedelta(days=5)  # inside 30-day window


def make_branch(name, last_commit_at=OLD_DATE, is_protected=False):
    return BranchInfo(
        name=name, last_commit_at=last_commit_at, is_protected=is_protected
    )


# -- target branch skipped --
def test_target_branch_skipped():
    b = make_branch("main")
    assert not _branch_is_stale(
        b,
        now=FIXED_NOW,
        max_age_days=30,
        target_branch="main",
        open_pr=set(),
        prefix_only=True,
        branch_prefix="mill/",
    )


# -- protected branch skipped --
def test_protected_branch_skipped():
    b = make_branch("mill/old-ticket", is_protected=True)
    assert not _branch_is_stale(
        b,
        now=FIXED_NOW,
        max_age_days=30,
        target_branch="main",
        open_pr=set(),
        prefix_only=True,
        branch_prefix="mill/",
    )


# -- branch with open PR skipped --
def test_open_pr_branch_skipped():
    b = make_branch("mill/old-ticket")
    assert not _branch_is_stale(
        b,
        now=FIXED_NOW,
        max_age_days=30,
        target_branch="main",
        open_pr={"mill/old-ticket"},
        prefix_only=True,
        branch_prefix="mill/",
    )


# -- branch newer than max_age_days skipped --
def test_recent_branch_skipped():
    b = make_branch("mill/recent", last_commit_at=RECENT_DATE)
    assert not _branch_is_stale(
        b,
        now=FIXED_NOW,
        max_age_days=30,
        target_branch="main",
        open_pr=set(),
        prefix_only=True,
        branch_prefix="mill/",
    )


# -- non-prefix branch skipped when prefix_only=True --
def test_non_prefix_skipped_when_prefix_only():
    b = make_branch("feature/old-stuff")
    assert not _branch_is_stale(
        b,
        now=FIXED_NOW,
        max_age_days=30,
        target_branch="main",
        open_pr=set(),
        prefix_only=True,
        branch_prefix="mill/",
    )


# -- non-prefix branch deleted when prefix_only=False --
def test_non_prefix_deleted_when_prefix_only_false():
    b = make_branch("feature/old-stuff")
    assert _branch_is_stale(
        b,
        now=FIXED_NOW,
        max_age_days=30,
        target_branch="main",
        open_pr=set(),
        prefix_only=False,
        branch_prefix="mill/",
    )


# -- old unprotected mill/* branch with no open PR selected for deletion --
def test_old_mill_branch_selected_for_deletion():
    b = make_branch("mill/abandoned-ticket")
    assert _branch_is_stale(
        b,
        now=FIXED_NOW,
        max_age_days=30,
        target_branch="main",
        open_pr=set(),
        prefix_only=True,
        branch_prefix="mill/",
    )


# ---------------------------------------------------------------------------
# Loop logic: per-repo cleanup with fake forge
# ---------------------------------------------------------------------------


class FakeForge:
    """A forge stub whose list_branches / list_open_pr_branches / delete_branch
    are controllable for testing the per-repo iteration."""

    def __init__(self, branches=(), open_pr=(), *, delete_raises=None):
        self._branches = list(branches)
        self._open_pr = set(open_pr)
        self._deleted: list[str] = []
        self._delete_raises = delete_raises  # branch name -> Exception

    def list_branches(self):
        return list(self._branches)

    def list_open_pr_branches(self):
        return set(self._open_pr)

    def delete_branch(self, *, branch: str) -> bool:
        if self._delete_raises and branch in self._delete_raises:
            raise self._delete_raises[branch]
        self._deleted.append(branch)
        return True

    @property
    def deleted(self) -> list[str]:
        return self._deleted


@pytest.mark.asyncio
async def test_per_repo_cleanup_deletes_only_eligible(
    settings, repo_config, monkeypatch
):
    """Drive one iteration of the per-repo logic with a fake forge,
    asserting delete_branch is called only for eligible branches."""
    from robotsix_mill.runtime.worker import Worker
    from robotsix_mill.stages import StageContext

    ctx = StageContext(settings=settings, service=None, repo_config=repo_config)
    # Enable the cleanup and set a branch prefix
    settings.stale_branch_cleanup_periodic = True
    settings.stale_branch_cleanup_interval_seconds = 3600
    settings.stale_branch_max_age_days = 30
    settings.stale_branch_cleanup_prefix_only = True
    settings.branch_prefix = "mill/"
    settings.forge_target_branch = "main"

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    recent = now - timedelta(days=5)

    branches = [
        BranchInfo(name="main", last_commit_at=old, is_protected=True),  # protected
        BranchInfo(name="mill/old-no-pr", last_commit_at=old, is_protected=False),
        BranchInfo(name="mill/recent", last_commit_at=recent, is_protected=False),
        BranchInfo(name="mill/has-pr", last_commit_at=old, is_protected=False),
        BranchInfo(name="feature/old-dev", last_commit_at=old, is_protected=False),
    ]
    open_pr = {"mill/has-pr"}

    fake = FakeForge(branches=branches, open_pr=open_pr)

    # Patch get_forge to return our fake, and get_repos_config to return
    # the current repo_config.
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda _settings, _rc: fake,
    )
    # The loop iterates repos from get_repos_config().repos.values().
    # We need to make it iterate our repo_config.
    from robotsix_mill.config import ReposRegistry

    class FakeReposRegistry(ReposRegistry):
        @property
        def repos(self):
            return {repo_config.repo_id: repo_config}

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.periodic_passes.get_repos_config",
        lambda: FakeReposRegistry(repos={repo_config.repo_id: repo_config}),
    )

    # Override _initial_delay to return 0 so the test doesn't sleep.
    monkeypatch.setattr(Worker, "_initial_delay", lambda self, kind, interval: 0.0)

    w = Worker(ctx)
    w._stale_branch_task = None

    # Run ONE iteration of the loop by spawning and cancelling after
    # the first sleep.
    loop_task = asyncio.create_task(w._stale_branch_cleanup_loop())

    # Wait long enough for one iteration to complete, then cancel.
    await asyncio.sleep(0.2)
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    # Only the old, unprotected, no-open-PR, prefix-matching branch
    # should be deleted.
    assert fake.deleted == ["mill/old-no-pr"]


@pytest.mark.asyncio
async def test_forge_exception_does_not_crash_loop(settings, repo_config, monkeypatch):
    """A forge that raises inside the per-repo block is caught and the
    loop continues without crashing."""
    from robotsix_mill.runtime.worker import Worker
    from robotsix_mill.stages import StageContext

    ctx = StageContext(settings=settings, service=None, repo_config=repo_config)
    settings.stale_branch_cleanup_periodic = True
    settings.stale_branch_cleanup_interval_seconds = 3600

    class RaisingForge:
        def list_branches(self):
            raise RuntimeError("API down")

        def list_open_pr_branches(self):
            return set()

        def delete_branch(self, *, branch: str) -> bool:
            return True

    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda _settings, _rc: RaisingForge(),
    )
    from robotsix_mill.config import ReposRegistry

    class FakeReposRegistry(ReposRegistry):
        @property
        def repos(self):
            return {repo_config.repo_id: repo_config}

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.periodic_passes.get_repos_config",
        lambda: FakeReposRegistry(repos={repo_config.repo_id: repo_config}),
    )
    monkeypatch.setattr(Worker, "_initial_delay", lambda self, kind, interval: 0.0)

    w = Worker(ctx)
    w._stale_branch_task = None

    loop_task = asyncio.create_task(w._stale_branch_cleanup_loop())
    await asyncio.sleep(0.2)

    # The loop should still be alive (not crashed).
    assert not loop_task.done()

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
