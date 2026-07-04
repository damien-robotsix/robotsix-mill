import subprocess
from pathlib import Path

import pytest

from robotsix_mill import sandbox
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    return Settings(**env)


# The PATH export sandbox.run() prepends to every effective command so
# that pip --user console scripts (installed under $HOME/.local/bin =
# /tmp/.local/bin) resolve in the sandbox.
PATH_EXPORT = 'export PATH="$HOME/.local/bin:/tmp/.local/bin:$PATH"; '


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
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")

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
    assert a[-3:] == ["python:3.14-slim", "-lc", PATH_EXPORT + "pytest -q"]


def test_sandbox_image_override(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        data_dir="/data",
        sandbox_image="python:3.14-slim",
        sandbox_proxy_url="",
    )
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    # Per-repo override wins over settings.sandbox_image.
    sandbox.run(
        "pytest -q",
        repo_dir="/data/work/repo",
        settings=s,
        sandbox_image="ros:rolling-ros-base",
    )
    a = seen["argv"]
    assert a[a.index("--entrypoint") + 1] == "sh"
    assert a[a.index("--entrypoint") + 2] == "ros:rolling-ros-base"

    # None (omitted) → fall back to settings.sandbox_image.
    sandbox.run("pytest -q", repo_dir="/data/work/repo", settings=s)
    a = seen["argv"]
    assert a[a.index("--entrypoint") + 2] == "python:3.14-slim"


def test_path_export_prepended_for_plain_and_install(tmp_path, monkeypatch):
    """The -lc command string must begin with the pip --user scripts-dir
    PATH export for BOTH a plain command and an install_project=True
    command, so console scripts (e.g. yamllint via extra_sandbox_packages)
    resolve instead of dying with rc=127."""
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

    # plain command (install_project=False)
    sandbox.run("yamllint --strict .", repo_dir=repo, settings=s)
    assert seen["argv"][-1].startswith(PATH_EXPORT)

    # install_project=True (the test gate)
    sandbox.run("yamllint --strict .", repo_dir=repo, settings=s, install_project=True)
    cmd = seen["argv"][-1]
    assert cmd.startswith(PATH_EXPORT)
    # the project install must run AFTER the export (so the export is in
    # effect for the install too)
    assert "pip install --user" in cmd[len(PATH_EXPORT) :]


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
        return subprocess.CompletedProcess(argv, 0, b"", b"")

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
        return subprocess.CompletedProcess(argv, 125, stdout="", stderr=b"no daemon")

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
    pairs = list(zip(a, a[1:], strict=False))
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
    pip = "pip install --user --quiet --disable-pip-version-check"
    assert cmd == (PATH_EXPORT + f"({pip} '.[dev]' || {pip} .) && pytest -q")


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
    assert seen["argv"][-1] == PATH_EXPORT + "pytest -q"


def test_install_project_noop_without_network(tmp_path, monkeypatch):
    """No egress proxy → install is still attempted (best-effort) but
    uses ``;`` so the command always runs regardless of install success.
    The sandbox may still have network (agent workspace); when it
    doesn't, pip fails fast with DNS error under --network none."""
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
    pip = "pip install --user --quiet --disable-pip-version-check"
    expected_tail = (
        f"({pip} '.[dev]' || {pip} .) ; pytest -q"
    )
    assert seen["argv"][-1].startswith(PATH_EXPORT)
    assert expected_tail in seen["argv"][-1]


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
    sandbox.run(
        "git status", repo_dir=repo, settings=s
    )  # default install_project=False
    assert seen["argv"][-1] == PATH_EXPORT + "git status"


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


# ── Dockerfile provisioning ───────────────────────────────────────────


def test_dockerfile_installs_github_cli():
    """The sandbox image must bake in the GitHub CLI (`gh`) via the
    official apt source so tickets can drive push -> PR -> merge
    reproducibly. Asserted against the Dockerfile text so it's testable
    without a Docker daemon (the sandbox has neither network nor daemon)."""
    dockerfile = (
        Path(__file__).resolve().parents[2] / "sandbox" / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "cli.github.com" in dockerfile
    assert "install -y --no-install-recommends gh" in dockerfile
    # curl must remain (it fetches the keyring and is used elsewhere)
    assert "curl" in dockerfile


# ── extra sandbox packages ────────────────────────────────────────────


def test_extra_packages_empty_list_no_prefix(tmp_path, monkeypatch):
    """Empty list → --read-only present, no install prefix in final command."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox, "load_extra_sandbox_packages", lambda repo_dir: [])
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    assert "--read-only" in a
    assert a[-1] == PATH_EXPORT + "true"


def test_extra_packages_pip_only_keeps_readonly(tmp_path, monkeypatch):
    """Only pip: packages → --read-only PRESENT, pip install in prefix, no apt."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox, "load_extra_sandbox_packages", lambda repo_dir: ["pip:requests"]
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    assert "--read-only" in a
    cmd = a[-1]
    assert "pip install --user" in cmd
    assert "requests" in cmd
    assert "apt-get" not in cmd


def test_tmp_tmpfs_mounted_exec(tmp_path, monkeypatch):
    """The /tmp tmpfs is mounted exec (Docker's default is noexec) so pip
    --user console scripts under $HOME/.local/bin can execute; nosuid/nodev
    hardening is retained."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox, "load_extra_sandbox_packages", lambda repo_dir: [])
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    tmpfs_indices = [i for i, x in enumerate(a) if x == "--tmpfs"]
    tmpfs_targets = {a[i + 1] for i in tmpfs_indices}
    assert "/tmp:exec,rw,nosuid,nodev" in tmpfs_targets
    assert "/tmp" not in tmpfs_targets


def test_extra_packages_apt_drops_readonly(tmp_path, monkeypatch):
    """Any apt package → --read-only ABSENT, tmpfs mounts for apt dirs added."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox, "load_extra_sandbox_packages", lambda repo_dir: ["colcon"]
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    assert "--read-only" not in a
    assert "--tmpfs" in a
    assert "/var/cache/apt" in a
    assert "/var/lib/apt/lists" in a
    assert "/var/lib/dpkg" in a


def test_extra_packages_apt_has_tmpfs_mounts(tmp_path, monkeypatch):
    """Apt packages → exact tmpfs paths are present in argv."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox, "load_extra_sandbox_packages", lambda repo_dir: ["apt:curl"]
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    # Each --tmpfs flag should be followed by its mount path
    tmpfs_indices = [i for i, x in enumerate(a) if x == "--tmpfs"]
    tmpfs_targets = {a[i + 1] for i in tmpfs_indices}
    assert tmpfs_targets >= {
        "/var/cache/apt",
        "/var/lib/apt/lists",
        "/var/lib/dpkg",
    }


def test_extra_packages_prefix_order(tmp_path, monkeypatch):
    """Apt install block runs before pip block, both before user command."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox,
        "load_extra_sandbox_packages",
        lambda repo_dir: ["colcon", "pip:requests"],
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    cmd = seen["argv"][-1]
    apt_pos = cmd.find("apt-get")
    pip_pos = cmd.find("pip install")
    cmd_pos = cmd.rfind("true")
    assert apt_pos != -1 and pip_pos != -1
    assert apt_pos < pip_pos < cmd_pos, (
        f"expected apt ({apt_pos}) < pip ({pip_pos}) < command ({cmd_pos}) in: {cmd}"
    )


def test_extra_packages_error_resilience(tmp_path, monkeypatch):
    """Prefix uses || echo "WARNING:" guards for each package."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox,
        "load_extra_sandbox_packages",
        lambda repo_dir: ["colcon", "pip:requests"],
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    cmd = seen["argv"][-1]
    assert '|| echo "WARNING: failed to install apt package:' in cmd
    assert '|| echo "WARNING: failed to install pip package:' in cmd


def test_extra_packages_bare_name_defaults_to_apt(tmp_path, monkeypatch):
    """Bare name colcon → treated as apt, not pip."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox, "load_extra_sandbox_packages", lambda repo_dir: ["colcon"]
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    cmd = seen["argv"][-1]
    assert "colcon" in cmd
    assert "apt-get install" in cmd
    assert "pip install" not in cmd


def test_extra_packages_prefix_stripped(tmp_path, monkeypatch):
    """pip:requests → pip install … requests (no 'pip:' in pkg name)."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox, "load_extra_sandbox_packages", lambda repo_dir: ["pip:requests"]
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    cmd = seen["argv"][-1]
    assert "requests" in cmd
    assert "pip:requests" not in cmd


def test_extra_packages_mixed_apt_and_pip(tmp_path, monkeypatch):
    """Both apt and pip blocks present in prefix."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox,
        "load_extra_sandbox_packages",
        lambda repo_dir: ["colcon", "pip:requests"],
    )
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    cmd = seen["argv"][-1]
    assert "apt-get" in cmd
    assert "pip install" in cmd


def test_extra_packages_missing_config_noop(tmp_path, monkeypatch):
    """load_extra_sandbox_packages returns [] when no config → no prefix, --read-only kept."""
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    monkeypatch.setattr(sandbox, "load_extra_sandbox_packages", lambda repo_dir: [])
    sandbox.run("true", repo_dir="/data/work/repo", settings=s)

    a = seen["argv"]
    assert "--read-only" in a
    assert a[-1] == PATH_EXPORT + "true"


# ── _has_uv_sources ──────────────────────────────────────────────────


def test_has_uv_sources_present(tmp_path):
    """pyproject.toml with a non-empty [tool.uv.sources] table → True."""

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.uv.sources]\n"
        "x = { git = 'https://github.com/org/x' }\n",
        encoding="utf-8",
    )
    assert sandbox._has_uv_sources(repo) is True


def test_has_uv_sources_absent(tmp_path):
    """pyproject.toml without [tool.uv.sources] → False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.uv]\ndev-dependencies = []\n",
        encoding="utf-8",
    )
    assert sandbox._has_uv_sources(repo) is False


def test_has_uv_sources_empty_table(tmp_path):
    """[tool.uv.sources] header but no keys → False (len(sources) > 0
    guard)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.uv.sources]\n[tool.uv.dev-dependencies]\n",
        encoding="utf-8",
    )
    assert sandbox._has_uv_sources(repo) is False


def test_has_uv_sources_pep508_git_dep_no_sources_table(tmp_path):
    """PEP 508 ``@ git+https://`` direct reference but no
    [tool.uv.sources] → True (pip can't resolve git deps)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\ndependencies = [\n"
        '  "some-pkg @ git+https://github.com/org/x.git@main",\n'
        "]\n",
        encoding="utf-8",
    )
    assert sandbox._has_uv_sources(repo) is True


def test_has_uv_sources_no_git_deps_no_sources_table(tmp_path):
    """No git deps and no [tool.uv.sources] → False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\ndependencies = ['requests>=2']\n",
        encoding="utf-8",
    )
    assert sandbox._has_uv_sources(repo) is False


def test_has_uv_sources_no_pyproject(tmp_path):
    """No pyproject.toml at all → False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert sandbox._has_uv_sources(repo) is False


def test_has_uv_sources_malformed_toml(tmp_path):
    """Malformed TOML → False (graceful degradation)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[broken =\n", encoding="utf-8"
    )
    assert sandbox._has_uv_sources(repo) is False


# ── _maybe_install_prefix with [tool.uv.sources] ─────────────────────


def test_maybe_install_prefix_uv_path(tmp_path):
    """With [tool.uv.sources] + uv.lock → emits uv sync --frozen --no-dev."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.uv.sources]\n"
        "x = { git = 'https://github.com/org/x' }\n",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("", encoding="utf-8")

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )

    prefix = sandbox._maybe_install_prefix("pytest -q", repo, s)
    assert "uv sync --frozen --no-dev --quiet" in prefix
    assert "command -v uv" in prefix
    assert "WARNING: uv not found, falling back to pip" in prefix
    assert prefix.endswith(" && pytest -q")


def test_maybe_install_prefix_pep508_git_dep(tmp_path):
    """PEP 508 ``@ git+https://`` (no [tool.uv.sources]) + uv.lock →
    emits uv sync --frozen --no-dev."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\ndependencies = [\n"
        '  "some-pkg @ git+https://github.com/org/x.git@main",\n'
        "]\n",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("", encoding="utf-8")

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )

    prefix = sandbox._maybe_install_prefix("pytest -q", repo, s)
    assert "uv sync --frozen --no-dev --quiet" in prefix
    assert "command -v uv" in prefix
    assert "WARNING: uv not found, falling back to pip" in prefix
    assert prefix.endswith(" && pytest -q")


def test_maybe_install_prefix_no_uv_sources_unchanged(tmp_path):
    """Without [tool.uv.sources] → emits pip install (unchanged behavior)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )

    prefix = sandbox._maybe_install_prefix("pytest -q", repo, s)
    pip = "pip install --user --quiet --disable-pip-version-check"
    assert f"({pip} '.[dev]' || {pip} .) && pytest -q" in prefix
    assert "uv sync" not in prefix


def test_maybe_install_prefix_uv_lock_missing(tmp_path):
    """[tool.uv.sources] present but no uv.lock → falls back to pip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.uv.sources]\n"
        "x = { git = 'https://github.com/org/x' }\n",
        encoding="utf-8",
    )
    # No uv.lock created.

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )

    prefix = sandbox._maybe_install_prefix("pytest -q", repo, s)
    pip = "pip install --user --quiet --disable-pip-version-check"
    assert f"({pip} '.[dev]' || {pip} .) && pytest -q" in prefix
    assert "uv sync" not in prefix


def test_maybe_install_prefix_uv_sources_no_proxy_noop(tmp_path):
    """[tool.uv.sources] present but no proxy → install is still
    attempted (best-effort) with ``;`` so the command always runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n[tool.uv.sources]\n"
        "x = { git = 'https://github.com/org/x' }\n",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("", encoding="utf-8")

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="",
    )

    prefix = sandbox._maybe_install_prefix("pytest -q", repo, s)
    pip = "pip install --user --quiet --disable-pip-version-check"
    assert "uv sync" in prefix
    assert f"({pip} '.[dev]' || {pip} .)" in prefix
    assert prefix.endswith("; pytest -q")


def test_maybe_install_prefix_uv_sources_no_pyproject_noop(tmp_path):
    """No pyproject.toml → command unchanged regardless of uv.lock."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "uv.lock").write_text("", encoding="utf-8")

    s = _settings(
        tmp_path,
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )

    prefix = sandbox._maybe_install_prefix("pytest -q", repo, s)
    assert prefix == "pytest -q"


def test_extra_packages_integration_from_config_file(tmp_path, monkeypatch):
    """End-to-end: config file → parser → prefix builder → argv."""
    repo = tmp_path / "ticket"
    config_dir = repo / ".robotsix-mill"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "extra_sandbox_packages: [colcon, pip:requests]\n", encoding="utf-8"
    )

    s = _settings(tmp_path, data_dir=str(tmp_path), sandbox_proxy_url="")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run("pytest -q", repo_dir=repo, settings=s)

    cmd = seen["argv"][-1]
    assert "apt-get update" in cmd
    assert "colcon" in cmd
    assert "pip install --user" in cmd
    assert "requests" in cmd
    assert cmd.endswith("pytest -q")


# --- orphan sandbox reaper -------------------------------------------------
# Containers leaked by a mill crash/restart mid-run (their timeout is
# parent-process enforced; --rm only fires on exit) are reaped by
# reap_orphan_sandboxes. These tests mock the docker CLI (subprocess.run).


def _completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def test_reap_orphan_sandboxes_startup_removes_all(monkeypatch):
    """max_age_seconds=None (startup) removes every matching container
    without inspecting ages — anything present is an orphan from before
    this process began."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return _completed(
                argv, 0, stdout="abc123\tmill-sbx-aaa\ndef456\tmill-fetch-bbb\n"
            )
        if argv[:3] == ["docker", "rm", "-f"]:
            return _completed(argv, 0)
        raise AssertionError(f"unexpected argv {argv}")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.reap_orphan_sandboxes() == 2
    assert not any(a[:2] == ["docker", "inspect"] for a in calls)
    removed = {a[3] for a in calls if a[:3] == ["docker", "rm", "-f"]}
    assert removed == {"abc123", "def456"}
    # The listing must use `docker ps -a` so Created/Exited restart-orphans
    # (invisible to a running-only `docker ps`) are swept at startup.
    ps_calls = [a for a in calls if a[:2] == ["docker", "ps"]]
    assert ps_calls and "-a" in ps_calls[0]


def test_reap_orphan_sandboxes_age_gated(monkeypatch):
    """A positive threshold removes only containers older than it."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    starts = {
        "old1": (now - timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
        "young1": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
    }
    removed = []

    def fake_run(argv, **kw):
        if argv[:2] == ["docker", "ps"]:
            return _completed(
                argv, 0, stdout="old1\tmill-sbx-old\nyoung1\tmill-sbx-young\n"
            )
        if argv[:2] == ["docker", "inspect"]:
            return _completed(argv, 0, stdout=starts[argv[-1]] + "\n")
        if argv[:3] == ["docker", "rm", "-f"]:
            removed.append(argv[3])
            return _completed(argv, 0)
        raise AssertionError(f"unexpected argv {argv}")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.reap_orphan_sandboxes(max_age_seconds=3600) == 1
    assert removed == ["old1"]


def test_reap_orphan_sandboxes_no_candidates(monkeypatch):
    def fake_run(argv, **kw):
        if argv[:2] == ["docker", "ps"]:
            return _completed(argv, 0, stdout="")
        raise AssertionError(f"unexpected argv {argv}")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.reap_orphan_sandboxes() == 0


def test_reap_orphan_sandboxes_best_effort_on_missing_docker(monkeypatch):
    """A missing/erroring docker CLI must never raise — startup and the
    poll loop both depend on this."""

    def fake_run(argv, **kw):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.reap_orphan_sandboxes() == 0
    assert sandbox.reap_orphan_sandboxes(max_age_seconds=10) == 0


def test_parse_docker_started_at():
    dt = sandbox._parse_docker_started_at("2026-06-18T20:34:45.483641388Z")
    assert dt is not None and dt.year == 2026 and dt.tzinfo is not None
    # zero value / blank / junk → None (treated as "leave it alone")
    assert sandbox._parse_docker_started_at("0001-01-01T00:00:00Z") is None
    assert sandbox._parse_docker_started_at("") is None
    assert sandbox._parse_docker_started_at("not-a-date") is None


# ---------------------------------------------------------------------------
# Deploy-mode helpers: ensure_sandbox_network / resolve_data_volume /
# the run() once-guard. All best-effort; gated on DOCKER_HOST at the
# call sites so the dev stack path is unchanged.
# ---------------------------------------------------------------------------


def test_ensure_sandbox_network_noop_without_proxy(tmp_path, monkeypatch):
    """No proxy configured → nothing to wire; returns True without docker."""
    s = _settings(tmp_path, sandbox_proxy_url="")

    def boom(*a, **k):
        raise AssertionError("docker must not be invoked when no proxy is set")

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    assert sandbox.ensure_sandbox_network(s) is True


def test_ensure_sandbox_network_creates_and_connects(tmp_path, monkeypatch):
    """Happy path: create the internal network then attach the proxy."""
    s = _settings(
        tmp_path,
        sandbox_proxy_url="http://sandbox-proxy:8888",
        sandbox_network="mill-sandbox-net",
    )
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.ensure_sandbox_network(s) is True
    assert calls[0] == [
        "docker",
        "network",
        "create",
        "--internal",
        "mill-sandbox-net",
    ]
    assert calls[1] == [
        "docker",
        "network",
        "connect",
        "mill-sandbox-net",
        "sandbox-proxy",
    ]


def test_ensure_sandbox_network_idempotent_already_exists(tmp_path, monkeypatch):
    """Existing network + already-attached proxy are treated as success."""
    s = _settings(tmp_path, sandbox_proxy_url="http://sandbox-proxy:8888")

    def fake_run(argv, **kw):
        if argv[:3] == ["docker", "network", "create"]:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="network mill-sandbox-net already exists"
            )
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="endpoint with name sandbox-proxy already exists in network",
        )

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.ensure_sandbox_network(s) is True


def test_ensure_sandbox_network_connect_failure_returns_false(tmp_path, monkeypatch):
    """A genuine connect failure (proxy not attached) returns False."""
    s = _settings(tmp_path, sandbox_proxy_url="http://sandbox-proxy:8888")

    def fake_run(argv, **kw):
        if argv[:3] == ["docker", "network", "create"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="No such container: sandbox-proxy"
        )

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.ensure_sandbox_network(s) is False


def test_ensure_sandbox_network_never_raises(tmp_path, monkeypatch):
    """A Docker CLI OSError is swallowed and reported as False."""
    s = _settings(tmp_path, sandbox_proxy_url="http://sandbox-proxy:8888")

    def boom(*a, **k):
        raise OSError("docker missing")

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    assert sandbox.ensure_sandbox_network(s) is False


def test_resolve_data_volume_named_volume(tmp_path, monkeypatch):
    """A named-volume mount sets data_volume and clears sandbox_data_mount."""
    s = _settings(tmp_path, data_dir="/data", sandbox_data_mount="/old/path")
    mounts = (
        '[{"Type":"volume","Name":"mill-data","Destination":"/data"},'
        '{"Type":"bind","Source":"/etc/x","Destination":"/app/config"}]'
    )
    monkeypatch.setattr(sandbox.socket, "gethostname", lambda: "abc123")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=mounts, stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.resolve_data_volume(s)
    assert s.data_volume == "mill-data"
    assert s.sandbox_data_mount is None


def test_resolve_data_volume_bind_mount(tmp_path, monkeypatch):
    """A bind mount at data_dir sets sandbox_data_mount to the host source."""
    s = _settings(tmp_path, data_dir="/data")
    mounts = '[{"Type":"bind","Source":"/srv/mill/.data","Destination":"/data"}]'
    monkeypatch.setattr(sandbox.socket, "gethostname", lambda: "abc123")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=mounts, stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.resolve_data_volume(s)
    assert s.sandbox_data_mount == "/srv/mill/.data"


def test_resolve_data_volume_no_match_leaves_unchanged(tmp_path, monkeypatch):
    """No mount matching data_dir → settings untouched."""
    s = _settings(tmp_path, data_dir="/data", data_volume="mill_data")
    mounts = '[{"Type":"volume","Name":"other","Destination":"/somewhere-else"}]'
    monkeypatch.setattr(sandbox.socket, "gethostname", lambda: "abc123")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=mounts, stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.resolve_data_volume(s)
    assert s.data_volume == "mill_data"
    assert s.sandbox_data_mount is None


def test_resolve_data_volume_never_raises(tmp_path, monkeypatch):
    """A docker inspect failure is swallowed and leaves settings unchanged."""
    s = _settings(tmp_path, data_dir="/data", data_volume="mill_data")
    monkeypatch.setattr(sandbox.socket, "gethostname", lambda: "abc123")

    def boom(*a, **k):
        raise OSError("docker missing")

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    sandbox.resolve_data_volume(s)  # must not raise
    assert s.data_volume == "mill_data"


def test_run_skips_network_setup_in_dev_mode(tmp_path, monkeypatch):
    """Without DOCKER_HOST, run() never touches the network once-guard —
    the dev stack path is unchanged."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(sandbox, "_SANDBOX_NET_READY", False)
    called = {"n": 0}

    def spy(settings):
        called["n"] += 1

    monkeypatch.setattr(sandbox, "_ensure_sandbox_network_once", spy)
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="http://p:8888")
    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b""),
    )
    sandbox.run("echo hi", repo_dir="/data/work/repo", settings=s)
    assert called["n"] == 0


def test_run_triggers_network_setup_in_deploy_mode(tmp_path, monkeypatch):
    """With DOCKER_HOST set and a proxy configured, run() invokes the
    once-guard."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://mill-socket-proxy:2375")
    monkeypatch.setattr(sandbox, "_SANDBOX_NET_READY", False)
    called = {"n": 0}

    def spy(settings):
        called["n"] += 1

    monkeypatch.setattr(sandbox, "_ensure_sandbox_network_once", spy)
    s = _settings(tmp_path, data_dir="/data", sandbox_proxy_url="http://p:8888")
    monkeypatch.setattr(
        sandbox, "_repo_mount", lambda repo_dir, settings: ["--mount", "x"]
    )
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b""),
    )
    sandbox.run("echo hi", repo_dir="/data/work/repo", settings=s)
    assert called["n"] == 1


def test_ensure_sandbox_network_once_sets_flag_only_on_success(tmp_path, monkeypatch):
    """The once-guard sets the flag only on success and never re-runs after."""
    monkeypatch.setattr(sandbox, "_SANDBOX_NET_READY", False)
    s = _settings(tmp_path, sandbox_proxy_url="http://p:8888")
    results = iter([False, True])
    runs = {"n": 0}

    def fake_ensure(settings):
        runs["n"] += 1
        return next(results)

    monkeypatch.setattr(sandbox, "ensure_sandbox_network", fake_ensure)
    # First call fails → flag stays False, retried next time.
    sandbox._ensure_sandbox_network_once(s)
    assert sandbox._SANDBOX_NET_READY is False
    # Second call succeeds → flag set.
    sandbox._ensure_sandbox_network_once(s)
    assert sandbox._SANDBOX_NET_READY is True
    # Third call is a no-op (no further ensure invocation).
    sandbox._ensure_sandbox_network_once(s)
    assert runs["n"] == 2
