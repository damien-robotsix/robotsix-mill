"""Startup sandbox tool-inventory self-check (log-only)."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

from robotsix_mill.sandbox import _inventory


def _fake_run(stdout: str = "", returncode: int = 0, stderr: str = ""):
    seen: dict = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run, seen


@pytest.mark.parametrize(
    ("image", "repo"),
    [
        (
            "ghcr.io/damien-robotsix/robotsix-mill:main",
            "ghcr.io/damien-robotsix/robotsix-mill",
        ),
        (
            "ghcr.io/damien-robotsix/robotsix-mill-sandbox",
            "ghcr.io/damien-robotsix/robotsix-mill-sandbox",
        ),
        ("localhost:5000/img:tag@sha256:abc", "localhost:5000/img"),
        ("python:3.14-slim", "python"),
    ],
)
def test_image_repository(image, repo):
    assert _inventory._image_repository(image) == repo


def test_probe_reports_present_and_missing(monkeypatch):
    run, seen = _fake_run("+uv\n+gh\n+git\n-actionlint\n-zizmor\n-uvx\n")
    monkeypatch.setattr(_inventory.subprocess, "run", run)
    present, missing = _inventory.probe_sandbox_tools("img:tag")
    assert present == ["uv", "gh", "git"]
    assert missing == ["actionlint", "zizmor", "uvx"]
    argv = seen["argv"]
    assert "--pull=never" in argv
    assert argv[argv.index("--entrypoint") + 1] == "sh"
    assert "img:tag" in argv
    assert argv[-len(_inventory.SANDBOX_TOOLS) :] == list(_inventory.SANDBOX_TOOLS)


def test_probe_returns_none_without_docker(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(_inventory.subprocess, "run", boom)
    assert _inventory.probe_sandbox_tools("img") is None


def test_probe_returns_none_on_docker_error(monkeypatch):
    run, _ = _fake_run(returncode=125, stderr="No such image")
    monkeypatch.setattr(_inventory.subprocess, "run", run)
    assert _inventory.probe_sandbox_tools("img") is None


def test_log_inventory_flags_pin_drift_and_missing_tools(monkeypatch, caplog):
    run, _ = _fake_run("+uv\n+gh\n+git\n-actionlint\n-zizmor\n-uvx\n")
    monkeypatch.setattr(_inventory.subprocess, "run", run)
    settings = SimpleNamespace(
        sandbox_image="ghcr.io/damien-robotsix/robotsix-mill:main"
    )
    with caplog.at_level(logging.INFO, logger="robotsix_mill.sandbox"):
        _inventory.log_sandbox_tool_inventory(settings)
    text = caplog.text
    assert (
        "config-pin-drift: sandbox_image=ghcr.io/damien-robotsix/robotsix-mill:main"
        in text
    )
    assert (
        "sandbox image ghcr.io/damien-robotsix/robotsix-mill:main: tools present"
        in text
    )
    assert "'actionlint', 'zizmor', 'uvx'" in text


def test_log_inventory_no_drift_for_sandbox_image(monkeypatch, caplog):
    out = "".join(f"+{t}\n" for t in _inventory.SANDBOX_TOOLS)
    run, _ = _fake_run(out)
    monkeypatch.setattr(_inventory.subprocess, "run", run)
    settings = SimpleNamespace(
        sandbox_image="ghcr.io/damien-robotsix/robotsix-mill-sandbox:main"
    )
    with caplog.at_level(logging.INFO, logger="robotsix_mill.sandbox"):
        _inventory.log_sandbox_tool_inventory(settings)
    assert "config-pin-drift" not in caplog.text
    assert "missing []" in caplog.text


def test_main_dockerfile_bakes_in_lint_tools_matching_sandbox_pins():
    """The mill image IS the production sandbox_image, so it must carry the
    same pinned tools as sandbox/Dockerfile: actionlint/zizmor, the node
    runtime (NODE_VERSION ARG lock-step), plus uvx.

    The node layers are duplicated across both Dockerfiles on purpose (the
    live sandbox runs the mill image, not the sandbox image — #3499), so the
    NODE_VERSION ARG is the drift guard: it must match in both files. curl
    has no sandbox-side pin (sandbox/Dockerfile installs it unpinned), so
    instead guard that the root Dockerfile's two apt layers (builder + base)
    agree on a single curl pin — a drift there would silently re-create the
    #3499 trap for the node tarball download path.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    main = (root / "Dockerfile").read_text(encoding="utf-8")
    sbx = (root / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    for line in sbx.splitlines():
        if line.startswith(
            (
                "ARG ACTIONLINT_VERSION=",
                "ARG ACTIONLINT_COMMIT=",
                "ARG ZIZMOR_VERSION=",
                "ARG NODE_VERSION=",
            )
        ) or (line.startswith("FROM golang:") and "AS actionlint-build" in line):
            assert line in main, line
    assert (
        "COPY --from=actionlint-build /out/actionlint /usr/local/bin/actionlint" in main
    )
    assert (
        "COPY --from=zizmor-build /opt/zizmor/bin/zizmor /usr/local/bin/zizmor" in main
    )
    assert "COPY --from=builder /usr/local/bin/uvx /usr/local/bin/uvx" in main
    assert "uvx --version && actionlint -version && zizmor --version" in main
    # curl pin parity within the root Dockerfile: the base stage needs a
    # pinned curl for the node tarball layer, and it must not drift from the
    # builder stage's curl pin.
    main_curl_pins = [
        ln.strip() for ln in main.splitlines() if ln.strip().startswith("curl=")
    ]
    assert len(main_curl_pins) == 2, main_curl_pins
    assert len(set(main_curl_pins)) == 1, main_curl_pins
    # sandbox/Dockerfile installs curl unpinned (uv installer + node tarball
    # layer); guard that it is still provisioned there so the node download
    # has a working curl.
    assert "curl ca-certificates" in sbx
