import subprocess

import pytest

from robotsix_mill import sandbox
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    return Settings(**env)


# --- local mode ---------------------------------------------------------

def test_local_runs_and_reports_exit_code(tmp_path):
    s = _settings(tmp_path, MILL_SANDBOX_MODE="local")
    rc, out = sandbox.run("echo hello", repo_dir=tmp_path, settings=s)
    assert rc == 0 and "hello" in out
    rc, _ = sandbox.run("exit 3", repo_dir=tmp_path, settings=s)
    assert rc == 3


def test_local_runs_in_repo_dir(tmp_path):
    (tmp_path / "marker").write_text("x")
    s = _settings(tmp_path, MILL_SANDBOX_MODE="local")
    rc, out = sandbox.run("ls", repo_dir=tmp_path, settings=s)
    assert rc == 0 and "marker" in out


def test_local_timeout_killed(tmp_path):
    s = _settings(tmp_path, MILL_SANDBOX_MODE="local", MILL_COMMAND_TIMEOUT="1")
    rc, out = sandbox.run("sleep 30", repo_dir=tmp_path, settings=s)
    assert rc == 124 and "timed out" in out


# --- docker mode (argv only — no daemon needed) -------------------------

def test_docker_argv_is_isolated(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        MILL_SANDBOX_MODE="docker",
        MILL_DATA_DIR="/data",
        MILL_DATA_VOLUME="mill_data",
        MILL_SANDBOX_IMAGE="python:3.14-slim",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    rc, out = sandbox.run("pytest -q", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    assert rc == 0 and out == "ok"
    assert a[:3] == ["docker", "run", "--rm"]
    assert "--network" in a and a[a.index("--network") + 1] == "none"
    assert "--read-only" in a
    assert "-v" in a and "mill_data:/data" in a
    assert a[a.index("-w") + 1] == "/data/work/repo"
    assert a[-3:] == ["sh", "-lc", "pytest -q"]


def test_docker_missing_raises_sandbox_error(tmp_path, monkeypatch):
    s = _settings(tmp_path, MILL_SANDBOX_MODE="docker")

    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("true", repo_dir=tmp_path, settings=s)


def test_docker_daemon_error_raises(tmp_path, monkeypatch):
    s = _settings(tmp_path, MILL_SANDBOX_MODE="docker")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 125, stdout="", stderr="no daemon")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("true", repo_dir=tmp_path, settings=s)
