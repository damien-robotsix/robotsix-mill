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

# ---------------------------------------------------------------------------
# Module-level guard: skip every test when Docker is unreachable
# ---------------------------------------------------------------------------

try:
    subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
except FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired:
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
    exit_code, output = sandbox.run("tree --version", repo_dir=repo_dir, settings=s)

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
    exit_code, output = sandbox.run("tree --version", repo_dir=repo_dir, settings=s)

    assert exit_code != 0, (
        f"tree --version should have failed (not installed), "
        f"but exit_code was 0:\n{output}"
    )
