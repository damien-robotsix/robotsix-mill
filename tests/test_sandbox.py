import subprocess

import pytest

from robotsix_mill import sandbox
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    return Settings(**env)


# Every command is always containerized — there is no local mode. These
# tests assert the isolation flags without needing a Docker daemon
# (subprocess.run is mocked).

def test_argv_is_isolated(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
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
    assert a[a.index("--entrypoint") + 1] == "sh"  # image ENTRYPOINT bypassed
    assert a[-3:] == ["python:3.14-slim", "-lc", "pytest -q"]


def test_sandbox_data_mount_overrides_volume(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        MILL_DATA_DIR="/data",
        MILL_DATA_VOLUME="mill_data",
        MILL_SANDBOX_DATA_MOUNT="/host/abs/.data",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)
    a = seen["argv"]
    assert "/host/abs/.data:/data" in a
    assert "mill_data:/data" not in a


def test_docker_missing_raises_sandbox_error(tmp_path, monkeypatch):
    s = _settings(tmp_path)

    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("true", repo_dir=tmp_path, settings=s)


def test_docker_daemon_error_raises(tmp_path, monkeypatch):
    s = _settings(tmp_path)

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 125, stdout="", stderr="no daemon")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("true", repo_dir=tmp_path, settings=s)
