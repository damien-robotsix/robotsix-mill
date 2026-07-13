"""Tests for ``src/robotsix_mill/stages/hooks.py``."""

import stat
from pathlib import Path

import pytest

from robotsix_mill.stages.hooks import run_prepare_hook


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_script(repo_dir: Path, content: str, executable: bool = True) -> Path:
    """Write ``.robotsix-mill/prepare`` into *repo_dir* and return its path."""
    hook_dir = repo_dir / ".robotsix-mill"
    hook_dir.mkdir(parents=True, exist_ok=True)
    script = hook_dir / "prepare"
    script.write_text(content, encoding="utf-8")
    if executable:
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------
# 1. Script absent
# ---------------------------------------------------------------------------


def test_script_absent_returns_none(tmp_path: Path):
    """When ``.robotsix-mill/prepare`` does not exist, return ``None``."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    result = run_prepare_hook(repo_dir, "ticket-1", workspace_dir)
    assert result is None


# ---------------------------------------------------------------------------
# 2. Script exits 0
# ---------------------------------------------------------------------------


def test_script_exits_zero_returns_none(tmp_path: Path):
    """A successful script (exit 0) returns ``None``."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    _write_script(repo_dir, "#!/bin/sh\necho ok\n")
    result = run_prepare_hook(repo_dir, "ticket-1", workspace_dir)
    assert result is None


# ---------------------------------------------------------------------------
# 3. Script exits non-zero
# ---------------------------------------------------------------------------


def test_script_exits_nonzero_returns_error(tmp_path: Path):
    """A failing script (exit 2) returns an error string with exit code
    and stderr."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    _write_script(repo_dir, "#!/bin/sh\necho fail >&2\nexit 2\n")
    result = run_prepare_hook(repo_dir, "ticket-1", workspace_dir)
    assert result is not None
    assert "exited 2" in result
    assert "fail" in result


# ---------------------------------------------------------------------------
# 4. Script not executable → made executable and run
# ---------------------------------------------------------------------------


def test_script_not_executable_is_fixed_and_run(tmp_path: Path):
    """A script without the +x bit is chmod'd and run successfully."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    _write_script(repo_dir, "#!/bin/sh\necho ok\n", executable=False)
    result = run_prepare_hook(repo_dir, "ticket-1", workspace_dir)
    assert result is None


# ---------------------------------------------------------------------------
# 5. Timeout
# ---------------------------------------------------------------------------


def test_script_timeout_returns_error(tmp_path: Path):
    """A script that sleeps beyond the 300 s timeout returns an error
    string containing "timed out"."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    # Use a tiny script that won't finish within the real test timeout
    # but we can't actually wait 300 s.  Monkeypatch the timeout to 0.1 s.
    _write_script(repo_dir, "#!/bin/sh\nsleep 10\n")
    import robotsix_mill.stages.hooks as hooks_mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(hooks_mod, "TIMEOUT_SECONDS", 1)
    try:
        result = run_prepare_hook(repo_dir, "ticket-1", workspace_dir)
        assert result is not None
        assert "timed out" in result
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# 6. Environment variables
# ---------------------------------------------------------------------------


def test_environment_variables_are_set(tmp_path: Path):
    """``TICKET_ID``, ``REPO_DIR``, ``WORKSPACE_DIR`` are exported to the script."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    out_file = tmp_path / "env_dump.txt"
    _write_script(
        repo_dir,
        f'#!/bin/sh\necho "$TICKET_ID" > {out_file}\n'
        f'echo "$REPO_DIR" >> {out_file}\n'
        f'echo "$WORKSPACE_DIR" >> {out_file}\n',
    )
    run_prepare_hook(repo_dir, "ticket-env-42", workspace_dir)
    lines = out_file.read_text().strip().split("\n")
    assert lines[0] == "ticket-env-42"
    assert lines[1] == str(repo_dir)
    assert lines[2] == str(workspace_dir)


# ---------------------------------------------------------------------------
# 7. Large stderr truncated
# ---------------------------------------------------------------------------


def test_large_stderr_truncated(tmp_path: Path):
    """More than 500 chars of stderr is truncated in the error string."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    big_msg = "x" * 700
    _write_script(repo_dir, f"#!/bin/sh\necho '{big_msg}' >&2\nexit 1\n")
    result = run_prepare_hook(repo_dir, "ticket-1", workspace_dir)
    assert result is not None
    assert "exited 1" in result
    # The stderr portion (after ": ") should be at most 500 chars + "…"
    # But the full result also includes the prefix "prepare hook exited 1: "
    # Let's just check the total length doesn't include the full 700 x's
    assert "x" * 700 not in result


# ---------------------------------------------------------------------------
# 8. Only config.yaml present, no prepare
# ---------------------------------------------------------------------------


def test_only_config_yaml_present_returns_none(tmp_path: Path):
    """When ``.robotsix-mill/config.yaml`` exists but ``prepare`` does not,
    return ``None``."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    hook_dir = repo_dir / ".robotsix-mill"
    hook_dir.mkdir()
    (hook_dir / "config.yaml").write_text("test_command: make test\n")
    result = run_prepare_hook(repo_dir, "ticket-1", workspace_dir)
    assert result is None
