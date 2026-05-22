import subprocess
from pathlib import Path

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

    # _repo_mount now checks repo_dir.exists(); mock it so the test
    # focuses on argv construction, not filesystem existence.
    monkeypatch.setattr(
        sandbox, "_repo_mount",
        lambda repo_dir, settings: [
            "--mount",
            f"type=volume,src=mill_data,dst=/data/work/repo,"
            f"volume-subpath=work/repo",
        ],
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    rc, out = sandbox.run("pytest -q", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    assert rc == 0 and out == "ok"
    assert a[:3] == ["docker", "run", "--rm"]
    assert "--network" in a and a[a.index("--network") + 1] == "none"
    assert "--read-only" in a
    # named-volume case: ONLY the ticket's repo sub-path, not the root
    assert (
        "type=volume,src=mill_data,dst=/data/work/repo,"
        "volume-subpath=work/repo" in a
    )
    assert "mill_data:/data" not in a  # data-dir root NOT exposed
    assert a[a.index("-w") + 1] == "/data/work/repo"
    assert a[a.index("--entrypoint") + 1] == "sh"  # image ENTRYPOINT bypassed
    assert a[-3:] == ["python:3.14-slim", "-lc", "pytest -q"]


def test_sandbox_never_exposes_management_plane(tmp_path, monkeypatch):
    """Regression (production-DB pollution incident): no bind/mount may
    expose the data-dir root, mill.db, the memory ledgers, or other
    tickets' workspaces — only THIS ticket's repo."""
    s = _settings(
        tmp_path, MILL_DATA_DIR="/data",
        MILL_SANDBOX_DATA_MOUNT="/host/.data",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    # _repo_mount now checks host_src.exists(); mock it so the test
    # focuses on isolation semantics, not filesystem existence.
    monkeypatch.setattr(
        sandbox, "_repo_mount",
        lambda repo_dir, settings: [
            "-v",
            "/host/.data/workspaces/T-1/repo:/data/workspaces/T-1/repo",
        ],
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("true", repo_dir="/data/workspaces/T-1/repo", settings=s)
    a = seen["argv"]
    binds = [a[i + 1] for i, x in enumerate(a) if x in ("-v", "--mount")]
    assert binds == [
        "/host/.data/workspaces/T-1/repo:/data/workspaces/T-1/repo"
    ]
    # no mount maps the data-dir root, and mill.db is never referenced
    assert not any(b.endswith(":/data") or "/host/.data:" in b for b in binds)
    assert "mill.db" not in " ".join(a)


def test_sandbox_refuses_repo_outside_data_dir(tmp_path):
    s = _settings(tmp_path, MILL_DATA_DIR="/data")
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("true", repo_dir="/etc", settings=s)
    with pytest.raises(sandbox.SandboxError):  # the data-dir root itself
        sandbox.run("true", repo_dir="/data", settings=s)


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

    # _repo_mount now checks host_src.exists(); mock it so the test
    # focuses on argv construction, not filesystem existence.
    monkeypatch.setattr(
        sandbox, "_repo_mount",
        lambda repo_dir, settings: [
            "-v",
            "/host/abs/.data/work/repo:/data/work/repo",
        ],
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)
    a = seen["argv"]
    # bind case: host repo sub-path only — NOT the data-dir root
    assert "/host/abs/.data/work/repo:/data/work/repo" in a
    assert "/host/abs/.data:/data" not in a
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


def test_repo_mount_rejects_non_existent_source(tmp_path):
    """When the repo directory doesn't exist (not yet cloned), _repo_mount
    raises SandboxError before Docker ever sees the mount spec."""
    s = _settings(
        tmp_path,
        MILL_DATA_DIR="/data",
        MILL_SANDBOX_DATA_MOUNT="/host/.data",
    )
    # bind case: the (container-visible) repo dir doesn't exist.
    # We deliberately check repo_dir, NOT the host path string — the
    # host path isn't visible in the mill container's filesystem
    # (false negative -> "every sandbox call fails as not cloned").
    with pytest.raises(sandbox.SandboxError, match="repo directory does not exist"):
        sandbox._repo_mount(Path("/data/work/repo"), s)

    # named-volume case: the repo dir doesn't exist
    s2 = _settings(
        tmp_path,
        MILL_DATA_DIR="/data",
        MILL_DATA_VOLUME="mill_data",
    )
    with pytest.raises(sandbox.SandboxError, match="repo directory does not exist"):
        sandbox._repo_mount(Path("/data/work/repo"), s2)


def test_sandbox_injects_pythonpath_for_src_layout(tmp_path, monkeypatch):
    """When repo_dir has a src/ subdirectory, sandbox.run injects
    -e PYTHONPATH=src so the mounted source takes priority over any
    stale copy baked into the sandbox image's site-packages."""
    repo = tmp_path / "ticket"
    repo.mkdir()
    (repo / "src").mkdir()

    s = _settings(
        tmp_path, MILL_DATA_DIR=str(tmp_path),
        MILL_SANDBOX_DATA_MOUNT=str(tmp_path),
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("pytest -q", repo_dir=repo, settings=s)

    a = seen["argv"]
    pairs = list(zip(a, a[1:]))
    assert ("-e", "PYTHONPATH=src") in pairs, (
        f"-e PYTHONPATH=src not found as a flag pair in argv: {a}"
    )


def test_sandbox_no_pythonpath_without_src_layout(tmp_path, monkeypatch):
    """When repo_dir has no src/ subdirectory, no PYTHONPATH env var is
    injected into the Docker argv."""
    repo = tmp_path / "ticket"
    repo.mkdir()

    s = _settings(
        tmp_path, MILL_DATA_DIR=str(tmp_path),
        MILL_SANDBOX_DATA_MOUNT=str(tmp_path),
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("pytest -q", repo_dir=repo, settings=s)

    a = seen["argv"]
    assert not any("PYTHONPATH" in str(x) for x in a)
