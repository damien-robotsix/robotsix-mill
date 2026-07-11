"""Unit tests for ``robotsix_mill.dev_tooling.autoupdate``.

Uses ``pytest`` + ``monkeypatch`` for mocking.  No ``unittest.mock``.
Tests never make real network calls or shell out to real ``git`` / ``docker``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import robotsix_mill.dev_tooling.autoupdate as au


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cp(
    ret: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Shorthand to build a ``CompletedProcess`` with *stdout*/*stderr*."""
    return subprocess.CompletedProcess([], ret, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# 1–2: Argument parsing
# ---------------------------------------------------------------------------


def test_parse_defaults() -> None:
    """Argument parser fills all defaults correctly."""
    ns = au._build_parser().parse_args([])
    assert ns.repo == str(Path.cwd())
    assert ns.state_dir is None
    assert ns.state_prefix == "mill-autoupdate"
    assert ns.remote == "origin/main"
    assert ns.service == "mill"
    assert ns.idle_check_cmd is None
    assert ns.ensure_branch is None
    assert ns.pre_build_wait == 1200
    assert ns.post_build_wait == 300
    assert ns.poll_interval == 90
    assert ns.max_deferrals == 4
    assert ns.no_idle_check is False


def test_parse_all_flags() -> None:
    """Every CLI flag is parsed and reflected in the namespace."""
    ns = au._build_parser().parse_args(
        [
            "--repo",
            "/tmp/my-repo",
            "--state-dir",
            "/tmp/state",
            "--state-prefix",
            "myapp",
            "--remote",
            "upstream/stable",
            "--service",
            "web",
            "--idle-check-cmd",
            "check.sh",
            "--ensure-branch",
            "main",
            "--pre-build-wait",
            "600",
            "--post-build-wait",
            "120",
            "--poll-interval",
            "30",
            "--max-deferrals",
            "2",
            "--no-force-deploy",
            "--no-idle-check",
        ]
    )
    assert ns.repo == "/tmp/my-repo"
    assert ns.state_dir == "/tmp/state"
    assert ns.state_prefix == "myapp"
    assert ns.remote == "upstream/stable"
    assert ns.service == "web"
    assert ns.idle_check_cmd == "check.sh"
    assert ns.ensure_branch == "main"
    assert ns.pre_build_wait == 600
    assert ns.post_build_wait == 120
    assert ns.poll_interval == 30
    assert ns.max_deferrals == 2
    assert ns.no_force_deploy is True
    assert ns.no_idle_check is True


# ---------------------------------------------------------------------------
# 3–5: SHA comparison
# ---------------------------------------------------------------------------


def test_sha_comparison_already_deployed(tmp_path: Path, monkeypatch) -> None:
    """When deployed SHA matches remote SHA, exit 0 with 'already on' log message."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    deployed = state / ".mill-autoupdate-deployed-sha"
    deployed.write_text("abc1234\n")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "git" and "rev-parse" in cmd:
            return _cp(0, "abc1234\n")
        if cmd[0] == "git" and "fetch" in cmd:
            return _cp(0)
        if cmd[0] == "git" and "status" in cmd:
            return _cp(0, "")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    log = state / "mill-autoupdate.log"
    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    log_text = log.read_text()
    assert "already on abc1234" in log_text


def test_sha_comparison_new_commits(tmp_path: Path, monkeypatch) -> None:
    """When SHAs differ, proceed past the SHA comparison (merge path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    deployed = state / ".mill-autoupdate-deployed-sha"
    deployed.write_text("abc1234\n")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "def9999\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "def9999\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "status" in cmd_str:
                return _cp(0, "")
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(1, "")  # .env changed
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "abc1234 some commit")
        if cmd[0] == "docker" and "build" in cmd:
            return _cp(0)
        if cmd[0] == "docker" and "up" in cmd:
            return _cp(0)
        if cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    # Verify merge was called
    assert any("merge" in " ".join(c) for c in calls)


def test_sha_comparison_first_run(tmp_path: Path, monkeypatch) -> None:
    """When no deployed-SHA file exists (first run), proceed to merge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "new1111\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "new1111\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "status" in cmd_str:
                return _cp(0, "")
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")  # .env unchanged
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "new1111 first commit")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in cmd:
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in cmd:
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    # deployed SHA should be recorded
    deployed = state / ".mill-autoupdate-deployed-sha"
    assert "new1111" in deployed.read_text()


# ---------------------------------------------------------------------------
# 6: Flock
# ---------------------------------------------------------------------------


def test_flock_already_held(tmp_path: Path, monkeypatch) -> None:
    """When lock is held by another process, exit 0 with 'another run' log message."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)
    # Make flock raise OSError (would block)
    monkeypatch.setattr(
        au.fcntl,
        "flock",
        lambda fd, op: (_ for _ in ()).throw(OSError(11, "Would block")),
    )

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    assert "another run in progress" in log.read_text()


# ---------------------------------------------------------------------------
# 7–8: Uncommitted changes
# ---------------------------------------------------------------------------


def test_uncommitted_changes_block(tmp_path: Path, monkeypatch) -> None:
    """When git status shows tracked changes (excluding .env), exit 0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git" and "status" in " ".join(cmd):
            return _cp(0, " M src/foo.py\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    assert "uncommitted changes" in log.read_text()


def test_uncommitted_changes_only_env_allowed(tmp_path: Path, monkeypatch) -> None:
    """When only .env is modified, proceed (don't block)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, " M .env\n")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "new2222\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "new2222\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in cmd:
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in cmd:
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    # Should have proceeded past the status check to fetch/merge
    assert any("fetch" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# 9: Fetch failure
# ---------------------------------------------------------------------------


def test_fetch_failure(tmp_path: Path, monkeypatch) -> None:
    """When git fetch fails, exit 1 with error log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git" and "status" in " ".join(cmd):
            return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "git" and "fetch" in " ".join(cmd):
            return _cp(1, "", "fatal: could not read from remote")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 1
    assert "git fetch failed" in log.read_text()


# ---------------------------------------------------------------------------
# 10: Merge ff-only failure
# ---------------------------------------------------------------------------


def test_merge_ff_only_failure(tmp_path: Path, monkeypatch) -> None:
    """When ff-only merge fails, exit 1 and restore .env backup."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("ORIGINAL_SECRET\n")
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "failing1\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(1, "")  # .env changed → backup
            if "checkout" in cmd_str and ".env" in cmd_str:
                return _cp(0)
            if "merge" in cmd_str:
                return _cp(1, "", "fatal: Not possible to fast-forward")
            if "log" in cmd_str:
                return _cp(0, "")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 1
    assert "ff-only merge failed" in log.read_text()
    # .env should be restored
    assert "ORIGINAL_SECRET" in (repo / ".env").read_text()


# ---------------------------------------------------------------------------
# 11: Docker build failure
# ---------------------------------------------------------------------------


def test_docker_build_failure(tmp_path: Path, monkeypatch) -> None:
    """When docker compose build fails, exit 1 with error log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "bbb1111\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "bbb1111\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(1, "", "Build failed")
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 1
    assert "docker compose build failed" in log.read_text()


# ---------------------------------------------------------------------------
# 12: Docker up failure
# ---------------------------------------------------------------------------


def test_docker_up_failure(tmp_path: Path, monkeypatch) -> None:
    """When docker compose up fails, exit 1 with error log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "ccc3333\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "ccc3333\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(1, "", "up failed")
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 1
    assert "docker compose up failed" in log.read_text()


# ---------------------------------------------------------------------------
# 13–14: Deferral counting
# ---------------------------------------------------------------------------


def test_deferral_counting(tmp_path: Path, monkeypatch) -> None:
    """After a busy-poll timeout, deferral counter increments; after N
    consecutive deferrals, force-deploy flag is set and deploy proceeds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"
    deferral_file = state / ".mill-autoupdate-deferrals"

    # Simulate: idle check always returns busy, pre-build wait is short.
    # After max_deferrals (4), the forced path should complete the deploy.

    # We'll run the main function once to build up deferrals.
    # To keep the test simple, simulate a pre-existing deferral file and
    # verify that when deferrals hit the cap, the deploy proceeds.

    # Write existing deferral count of 3
    deferral_file.write_text("3\n")

    # Deployed SHA exists (so we go into the poll path)
    deployed = state / ".mill-autoupdate-deployed-sha"
    deployed.write_text("old1111\n")

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "new4444\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "new4444\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    # Idle check always says busy
    monkeypatch.setattr(au, "_idle_check", lambda cmd, timeout=15: False)
    # Short poll interval to make the test fast
    monkeypatch.setattr(au.time, "sleep", lambda s: None)

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--state-prefix",
            "mill-autoupdate",
            "--idle-check-cmd",
            "false",
            "--pre-build-wait",
            "5",
            "--poll-interval",
            "1",
            "--max-deferrals",
            "4",
        ]
    )
    # With 3 existing deferrals + 1 more = 4 ≥ 4 → force deploy → success
    assert rc == 0
    # Deployed SHA should be recorded (force deploy happened)
    assert "new4444" in deployed.read_text()
    # Deferral file should be removed after success
    assert not deferral_file.exists()
    assert "FORCING" in log.read_text()


def test_deferral_reset_on_success(tmp_path: Path, monkeypatch) -> None:
    """After a successful deploy, deferral file is removed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    deferral_file = state / ".mill-autoupdate-deferrals"
    deferral_file.write_text("2\n")  # pre-existing deferrals

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "ddd5555\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "ddd5555\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    assert not deferral_file.exists()


def test_no_force_deploy_defers_even_at_cap(tmp_path: Path, monkeypatch) -> None:
    """When --no-force-deploy is set, hitting max_deferrals defers instead of
    force-deploying."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"
    deferral_file = state / ".mill-autoupdate-deferrals"

    # Pre-existing deferral count at cap (3 + 1 new = 4 = max)
    deferral_file.write_text("3\n")

    deployed = state / ".mill-autoupdate-deployed-sha"
    deployed.write_text("old1111\n")

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "new4444\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "new4444\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker":
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au, "_idle_check", lambda cmd, timeout=15: False)
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--state-prefix",
            "mill-autoupdate",
            "--idle-check-cmd",
            "false",
            "--pre-build-wait",
            "5",
            "--poll-interval",
            "1",
            "--max-deferrals",
            "4",
            "--no-force-deploy",
        ]
    )
    # Should defer (return 0) rather than force-deploy.
    assert rc == 0
    # Deploy should NOT have happened — SHA unchanged.
    assert "old1111" in deployed.read_text()
    assert "new4444" not in deployed.read_text()
    # Log should mention --no-force-deploy.
    assert "--no-force-deploy" in log.read_text()


# ---------------------------------------------------------------------------
# 15–16: Idle check helpers
# ---------------------------------------------------------------------------


def test_idle_check_busy(monkeypatch) -> None:
    """When idle-check command exits 0, _idle_check returns False."""

    def fake_run(cmd, **kwargs):
        return _cp(0)

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    assert au._idle_check("true") is False


def test_idle_check_idle(monkeypatch) -> None:
    """When idle-check command exits non-zero, _idle_check returns True."""

    def fake_run(cmd, **kwargs):
        return _cp(1)

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    assert au._idle_check("false") is True


# ---------------------------------------------------------------------------
# 17–18: --no-idle-check / no --idle-check-cmd skip polling
# ---------------------------------------------------------------------------


def test_no_idle_check_skips_polling(tmp_path: Path, monkeypatch) -> None:
    """When --no-idle-check is set, skip all idle polling (neither pre-build nor post-build)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()

    idle_called = False

    def fake_idle_check(cmd, timeout=15):
        nonlocal idle_called
        idle_called = True
        return True

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "eee6666\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "eee6666\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    monkeypatch.setattr(au, "_idle_check", fake_idle_check)
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--idle-check-cmd",
            "echo busy",  # would be used if no-idle-check weren't set
            "--no-idle-check",
        ]
    )
    assert rc == 0
    assert not idle_called


def test_no_idle_check_cmd_skips_polling(tmp_path: Path, monkeypatch) -> None:
    """When --idle-check-cmd is not provided, skip all idle polling."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()

    idle_called = False

    def fake_idle_check(cmd, timeout=15):
        nonlocal idle_called
        idle_called = True
        return True

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "fff7777\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "fff7777\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    monkeypatch.setattr(au, "_idle_check", fake_idle_check)
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state)])
    assert rc == 0
    assert not idle_called


# ---------------------------------------------------------------------------
# 19–20: .env backup / restore
# ---------------------------------------------------------------------------


def test_env_backup_and_restore(tmp_path: Path, monkeypatch) -> None:
    """When origin changes .env, local .env is backed up, merge proceeds, backup is restored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("MY_SECRET=keepme\n")
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "ggg8888\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "ggg8888\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(1, "")  # .env changed by origin
            if "checkout" in cmd_str and ".env" in cmd_str:
                # git checkout -- .env → detach it (simulate success)
                return _cp(0)
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    # .env should be restored verbatim
    assert (repo / ".env").read_text() == "MY_SECRET=keepme\n"
    # Log mentions backup
    assert "saved current .env" in log.read_text()
    assert "restored your .env" in log.read_text()


def test_env_not_touched_when_unchanged(tmp_path: Path, monkeypatch) -> None:
    """When origin does NOT change .env, no backup/restore cycle occurs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("MY_SECRET=keepme\n")
    state = tmp_path / "state"
    state.mkdir()
    log = state / "mill-autoupdate.log"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "hhh9999\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "hhh9999\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")  # .env unchanged
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    assert "saved current .env" not in log.read_text()
    assert "restored your .env" not in log.read_text()


# ---------------------------------------------------------------------------
# 21: --ensure-branch
# ---------------------------------------------------------------------------


def test_ensure_branch_checkout(tmp_path: Path, monkeypatch) -> None:
    """When --ensure-branch BRANCH is set, git checkout BRANCH is called before fetch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "checkout" in cmd_str and "main" in cmd_str:
                return _cp(0)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "iii0000\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "iii0000\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--ensure-branch",
            "main",
            "--no-idle-check",
        ]
    )
    assert rc == 0
    # Verify checkout was called before fetch
    checkout_idx = next(
        i for i, c in enumerate(calls) if "checkout" in " ".join(c) and "main" in c
    )
    fetch_idx = next(i for i, c in enumerate(calls) if "fetch" in " ".join(c))
    assert checkout_idx < fetch_idx


# ---------------------------------------------------------------------------
# 22: Log format
# ---------------------------------------------------------------------------


def test_log_format(tmp_path: Path) -> None:
    """Log lines contain [YYYY-MM-DD HH:MM:SS] prefix."""
    log_path = tmp_path / "test.log"
    au._log(log_path, "hello world")
    content = log_path.read_text()
    # Should start with timestamp prefix
    assert content.startswith("[20")
    assert "] hello world" in content


# ---------------------------------------------------------------------------
# 23: Deployed SHA recorded
# ---------------------------------------------------------------------------


def test_deployed_sha_recorded(tmp_path: Path, monkeypatch) -> None:
    """After successful deploy, the new remote SHA is written to the deployed-SHA file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    deployed = state / ".mill-autoupdate-deployed-sha"

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "status" in cmd_str:
                return _cp(0, "")
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "jjj1111\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "jjj1111\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "")
        if isinstance(cmd, list) and cmd[0] == "docker" and "build" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "docker" and "up" in " ".join(cmd):
            return _cp(0)
        if isinstance(cmd, list) and cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 0
    assert deployed.read_text().strip() == "jjj1111"


# ---------------------------------------------------------------------------
# 24: Remote flag parsing
# ---------------------------------------------------------------------------


def test_remote_flag_parsing() -> None:
    """--remote 'upstream/stable' splits into remote='upstream', branch='stable' (spec #24)."""
    ns = au._build_parser().parse_args(["--remote", "upstream/stable"])
    assert ns.remote == "upstream/stable"

    # Mirror run()'s parsing: first slash separates remote from branch.
    remote_name, _, remote_branch = ns.remote.partition("/")
    assert remote_name == "upstream"
    assert remote_branch == "stable"

    # The default value parses to (origin, main) — the shipped cron path.
    default = au._build_parser().parse_args([])
    remote_name, _, remote_branch = default.remote.partition("/")
    assert remote_name == "origin"
    assert remote_branch == "main"

    # Branch names containing slashes keep their full branch part.
    ns3 = au._build_parser().parse_args(["--remote", "origin/feature/x"])
    remote_name, _, remote_branch = ns3.remote.partition("/")
    assert remote_name == "origin"
    assert remote_branch == "feature/x"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fake_flock(monkeypatch) -> None:
    """Patch fcntl.flock to always succeed (lock acquired)."""
    monkeypatch.setattr(au.fcntl, "flock", lambda fd, op: None)


# ---------------------------------------------------------------------------
# Diverged deploy branch: --ensure-branch reconciles via reset --hard
# ---------------------------------------------------------------------------


def test_diverged_branch_reconciles_with_ensure_branch(
    tmp_path: Path, monkeypatch
) -> None:
    """When the ff-only merge fails (diverged) AND --ensure-branch is set,
    the deploy mirror is reconciled by `git reset --hard <remote>/<branch>`
    and the build proceeds (rc 0, deployed SHA recorded)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".mill-autoupdate-deployed-sha").write_text("abc1234\n")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "def9999\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "def9999\n")
            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                return _cp(0, "deadbeefcafe\n")  # diverged local tip
            if "fetch" in cmd_str:
                return _cp(0)
            if "checkout" in cmd_str:
                return _cp(0)
            if "status" in cmd_str:
                return _cp(0, "")
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")  # .env unchanged
            if "merge" in cmd_str:
                return _cp(1, "", "Not possible to fast-forward, aborting.")
            if "reset" in cmd_str:
                return _cp(0)
            if "log" in cmd_str:
                return _cp(0, "abc1234 some commit")
        if cmd[0] == "docker" and "build" in cmd:
            return _cp(0)
        if cmd[0] == "docker" and "up" in cmd:
            return _cp(0)
        if cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--ensure-branch",
            "main",
            "--no-idle-check",
        ]
    )
    assert rc == 0
    # reset --hard origin/main must have run after the failed merge.
    assert any(
        c[0] == "git" and "reset" in c and "--hard" in c and "origin/main" in c
        for c in calls
    ), "expected `git reset --hard origin/main` reconcile"
    # And the deploy proceeded.
    assert any(c[0] == "docker" and "build" in c for c in calls)
    assert "def9999" in (state / ".mill-autoupdate-deployed-sha").read_text()


def test_diverged_branch_without_ensure_branch_skips(
    tmp_path: Path, monkeypatch
) -> None:
    """Without --ensure-branch, a failed ff-only merge skips conservatively
    (rc 1, no reset, no build) — the legacy behavior for a manual run on an
    arbitrary branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=xyz")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".mill-autoupdate-deployed-sha").write_text("abc1234\n")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "git":
            cmd_str = " ".join(cmd)
            if "rev-parse" in cmd_str and "origin/main" in cmd_str:
                return _cp(0, "def9999\n")
            if "rev-parse" in cmd_str and "--short" in cmd_str:
                return _cp(0, "def9999\n")
            if "fetch" in cmd_str:
                return _cp(0)
            if "status" in cmd_str:
                return _cp(0, "")
            if "diff" in cmd_str and ".env" in cmd_str:
                return _cp(0, "")
            if "merge" in cmd_str:
                return _cp(1, "", "Not possible to fast-forward, aborting.")
        if cmd[0] == "getent":
            return _cp(0, "docker:x:999:\n")
        return _cp(0, "")

    monkeypatch.setattr(au.subprocess, "run", fake_run)
    _fake_flock(monkeypatch)
    monkeypatch.setattr(au.os, "open", lambda *a, **kw: 3)
    monkeypatch.setattr(au.os, "close", lambda fd: None)

    rc = au.main(["--repo", str(repo), "--state-dir", str(state), "--no-idle-check"])
    assert rc == 1
    assert not any(c[0] == "git" and "reset" in c for c in calls)
    assert not any(c[0] == "docker" and "build" in c for c in calls)
