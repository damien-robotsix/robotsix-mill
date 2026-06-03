import subprocess
from pathlib import Path

import pytest

from robotsix_mill import sandbox
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    return Settings(**env)


# Every command is always containerized — there is no local mode. These
# tests assert the isolation flags without needing a Docker daemon
# (subprocess.run is mocked).


def test_argv_is_isolated(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        data_dir="/data",
        data_volume="mill_data",
        sandbox_image="python:3.14-slim",
        sandbox_proxy_url="",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    # _repo_mount now checks repo_dir.exists(); mock it so the test
    # focuses on argv construction, not filesystem existence.
    monkeypatch.setattr(
        sandbox,
        "_repo_mount",
        lambda repo_dir, settings: [
            "--mount",
            "type=volume,src=mill_data,dst=/data/work/repo,volume-subpath=work/repo",
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
    assert "type=volume,src=mill_data,dst=/data/work/repo,volume-subpath=work/repo" in a
    assert "mill_data:/data" not in a  # data-dir root NOT exposed
    assert a[a.index("-w") + 1] == "/data/work/repo"
    assert a[a.index("--entrypoint") + 1] == "sh"  # image ENTRYPOINT bypassed
    assert a[-3:] == ["python:3.14-slim", "-lc", "pytest -q"]


def test_proxy_env_includes_no_proxy_for_loopback(tmp_path, monkeypatch):
    """When an egress proxy is configured, the sandbox must also set
    NO_PROXY for loopback so a repo's own localhost test server (e.g.
    auto-mail's test_server.py) isn't routed to the filtering proxy."""
    s = _settings(
        tmp_path,
        data_dir="/data",
        sandbox_image="python:3.14-slim",
        sandbox_network="mill-sandbox-net",
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    assert "HTTP_PROXY=http://sandbox-proxy:8888" in a
    # loopback bypasses the proxy in both case spellings
    assert "NO_PROXY=localhost,127.0.0.1,::1" in a
    assert "no_proxy=localhost,127.0.0.1,::1" in a


def test_sandbox_never_exposes_management_plane(tmp_path, monkeypatch):
    """Regression (production-DB pollution incident): no bind/mount may
    expose the data-dir root, mill.db, the memory ledgers, or other
    tickets' workspaces — only THIS ticket's repo."""
    s = _settings(
        tmp_path,
        data_dir="/data",
        sandbox_data_mount="/host/.data",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    # _repo_mount now checks host_src.exists(); mock it so the test
    # focuses on isolation semantics, not filesystem existence.
    monkeypatch.setattr(
        sandbox,
        "_repo_mount",
        lambda repo_dir, settings: [
            "-v",
            "/host/.data/workspaces/T-1/repo:/data/workspaces/T-1/repo",
        ],
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("true", repo_dir="/data/workspaces/T-1/repo", settings=s)
    a = seen["argv"]
    binds = [a[i + 1] for i, x in enumerate(a) if x in ("-v", "--mount")]
    assert binds == ["/host/.data/workspaces/T-1/repo:/data/workspaces/T-1/repo"]
    # no mount maps the data-dir root, and mill.db is never referenced
    assert not any(b.endswith(":/data") or "/host/.data:" in b for b in binds)
    assert "mill.db" not in " ".join(a)


def test_sandbox_refuses_repo_outside_data_dir(tmp_path):
    s = _settings(tmp_path, data_dir="/data")
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("true", repo_dir="/etc", settings=s)
    with pytest.raises(sandbox.SandboxError):  # the data-dir root itself
        sandbox.run("true", repo_dir="/data", settings=s)


def test_sandbox_data_mount_overrides_volume(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        data_dir="/data",
        data_volume="mill_data",
        sandbox_data_mount="/host/abs/.data",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    # _repo_mount now checks host_src.exists(); mock it so the test
    # focuses on argv construction, not filesystem existence.
    monkeypatch.setattr(
        sandbox,
        "_repo_mount",
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


def test_sandbox_resolves_relative_repo_dir_to_absolute(tmp_path, monkeypatch):
    """Regression: a relative repo_dir + relative data_dir used to flow
    through to docker run as ``-w .data/.../repo``, which Docker rejects
    with "needs to be an absolute path" (the BLOCKED bc-check ticket on
    2026-05-29 08:00 hit exactly this). The sandbox must call resolve()
    on both before emitting argv."""
    abs_data = tmp_path / "fake_data"
    abs_repo = abs_data / "board/work/repo"
    abs_repo.mkdir(parents=True)

    # Build settings with relative-style paths via attribute mutation so
    # we bypass Settings() YAML loading (which would need cwd=repo-root).
    s = _settings(tmp_path)
    s.data_dir = abs_data  # absolute, but we'll feed run() a relative path
    s.data_volume = "mill_data"

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.chdir(abs_data.parent)  # safe: no YAML load happens here
    # Feed a RELATIVE repo_dir; the new code must resolve it to abs_repo.
    sandbox.run(
        "true",
        repo_dir=Path("fake_data/board/work/repo"),
        settings=s,
    )
    a = seen["argv"]
    workdir = a[a.index("-w") + 1]
    assert Path(workdir).is_absolute(), f"workdir must be absolute, got {workdir!r}"
    assert workdir == str(abs_repo)


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
        data_dir="/data",
        sandbox_data_mount="/host/.data",
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
        data_dir="/data",
        data_volume="mill_data",
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
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
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


def test_install_project_prefixes_pip_when_pyproject_and_proxy(tmp_path, monkeypatch):
    """The test gate (install_project=True) must prepend a --user pip
    install of the repo so its DECLARED deps are importable — the gate
    otherwise runs against the image's frozen site-packages and a
    new-dependency ticket fails forever with ModuleNotFoundError."""
    repo = tmp_path / "ticket"
    (repo / "src").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("pytest -q", repo_dir=repo, settings=s, install_project=True)

    cmd = seen["argv"][-1]
    assert cmd == "pip install --user --quiet --disable-pip-version-check . && pytest -q"


def test_install_project_noop_without_pyproject(tmp_path, monkeypatch):
    """No pyproject → nothing to install; command runs unchanged."""
    repo = tmp_path / "ticket"
    repo.mkdir()
    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("pytest -q", repo_dir=repo, settings=s, install_project=True)
    assert seen["argv"][-1] == "pytest -q"


def test_install_project_noop_without_network(tmp_path, monkeypatch):
    """No egress proxy → pip can't reach PyPI; skip the install rather
    than turn a runnable gate into a guaranteed failure."""
    repo = tmp_path / "ticket"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("pytest -q", repo_dir=repo, settings=s, install_project=True)
    assert seen["argv"][-1] == "pytest -q"


def test_install_project_off_by_default(tmp_path, monkeypatch):
    """Other sandbox.run callers (agent run_command, merge) must NOT
    trigger a pip install — only the gate opts in."""
    repo = tmp_path / "ticket"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("git status", repo_dir=repo, settings=s)  # default install_project=False
    assert seen["argv"][-1] == "git status"


def test_sandbox_no_pythonpath_without_src_layout(tmp_path, monkeypatch):
    """When repo_dir has no src/ subdirectory, no PYTHONPATH env var is
    injected into the Docker argv."""
    repo = tmp_path / "ticket"
    repo.mkdir()

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("pytest -q", repo_dir=repo, settings=s)

    a = seen["argv"]
    assert not any("PYTHONPATH" in str(x) for x in a)
