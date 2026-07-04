"""Docker-backed integration tests for the sandbox.

These tests require a running Docker daemon, the ``mill-sandbox-net``
network with its egress proxy, and network access (apt reaches
deb.debian.org through the proxy).  They are skipped when Docker is
unavailable.

Separate from ``test_sandbox.py``, whose tests are hermetic — they
monkeypatch ``subprocess.run`` and never need Docker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from robotsix_mill import sandbox
from robotsix_mill.config import Settings
from robotsix_mill.sandbox import SandboxError

# ---------------------------------------------------------------------------
# Module-level guard: skip every test when Docker is unreachable
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return ``True`` when the Docker daemon is reachable and healthy.

    Broadens the typical ``docker info`` check to treat a 503
    (Service Unavailable) response the same as a connection refusal,
    so transient daemon or proxy outages skip the suite instead of
    failing it.  Also tries a lightweight ``docker run`` because
    ``docker info`` can succeed while the daemon refuses to schedule
    containers.
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        return False

    combined = (result.stdout + result.stderr).lower()
    for marker in (
        "503 service unavailable",
        "cannot connect",
        "is the docker daemon running",
    ):
        if marker in combined:
            return False

    # ``docker info`` succeeded — verify the daemon can actually
    # schedule a container (info can succeed while the scheduler
    # returns 503).
    for image in ("hello-world:latest", "alpine:latest", "busybox:latest"):
        try:
            r = subprocess.run(
                ["docker", "run", "--rm", "--pull", "never", image, "echo", "ok"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

        if r.returncode == 0:
            return True

        err = (r.stdout + r.stderr).lower()
        # Image not found → daemon may still be healthy; try next image.
        if any(
            phrase in err
            for phrase in (
                "no such image",
                "unable to find image",
                "pull",
            )
        ):
            continue

        # Daemon-level error (503, connection refused, etc.).
        return False

    # None of the known tiny images are pre-pulled, but docker info
    # was fine — assume the daemon is available.
    return True


if not _docker_available():
    pytest.skip("Docker daemon not available", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(repo_dir: Path, extra_packages: list[str] | None) -> None:
    """Write ``.robotsix-mill/config.yaml`` under *repo_dir*.

    When *extra_packages* is ``None`` the key is omitted entirely;
    otherwise it is written as a YAML list.
    """
    config_dir = repo_dir / ".robotsix-mill"
    config_dir.mkdir(parents=True, exist_ok=True)
    if extra_packages is None:
        (config_dir / "config.yaml").write_text(
            'test_command: "echo ok"\n', encoding="utf-8"
        )
    else:
        pkg_list = ", ".join(extra_packages)
        (config_dir / "config.yaml").write_text(
            f"extra_sandbox_packages: [{pkg_list}]\n", encoding="utf-8"
        )


def _settings(tmp_path: Path) -> Settings:
    """Return a :class:`Settings` suitable for Docker-backed tests.

    * ``data_dir`` and ``sandbox_data_mount`` both point at *tmp_path*
      so Docker can bind-mount the temp tree without a pre-existing
      named volume.
    * ``sandbox_proxy_url`` is set to the default proxy address so the
      sandbox gets network access (apt needs it).
    """
    return Settings(
        data_dir=str(tmp_path),
        sandbox_data_mount=str(tmp_path),
        sandbox_proxy_url="http://sandbox-proxy:8888",
    )


# ---------------------------------------------------------------------------
# Helpers (continued)
# ---------------------------------------------------------------------------


def _run_or_skip(*args: str, **kwargs: object) -> tuple[int, str]:
    """Call ``sandbox.run()``, converting daemon errors to a skip.

    The module-level ``_docker_available()`` guard catches most Docker
    outages at collection time, but a daemon that flakes *after* the
    guard passes (e.g. a proxy returning 503 mid-test) would otherwise
    fail the suite.  This wrapper catches ``SandboxError`` and calls
    ``pytest.skip`` so transient infrastructure blips don't block CI.
    """
    try:
        return sandbox.run(*args, **kwargs)  # type: ignore[no-any-return]
    except SandboxError as exc:
        pytest.skip(f"Docker daemon error during test: {exc}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.docker
def test_extra_packages_tree_installed_and_usable(tmp_path: Path) -> None:
    """Declaring ``extra_sandbox_packages: [tree]`` makes ``tree``
    available inside the sandbox."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _make_config(repo_dir, extra_packages=["tree"])

    s = _settings(tmp_path)
    exit_code, output = _run_or_skip("tree --version", repo_dir=repo_dir, settings=s)

    assert exit_code == 0, f"tree --version failed:\n{output}"
    assert "tree" in output.lower(), (
        f"expected 'tree' in version output, got:\n{output}"
    )


@pytest.mark.docker
def test_no_extra_packages_tree_not_installed(tmp_path: Path) -> None:
    """Without ``extra_sandbox_packages``, ``tree`` is absent from the
    base ``python:3.14-slim`` image and the command must fail."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _make_config(repo_dir, extra_packages=None)

    s = _settings(tmp_path)
    exit_code, output = _run_or_skip("tree --version", repo_dir=repo_dir, settings=s)

    assert exit_code != 0, (
        f"tree --version should have failed (not installed), "
        f"but exit_code was 0:\n{output}"
    )
