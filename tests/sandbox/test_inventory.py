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
    same pinned actionlint/zizmor as sandbox/Dockerfile, plus uvx."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    main = (root / "Dockerfile").read_text(encoding="utf-8")
    sbx = (root / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    for line in sbx.splitlines():
        if line.startswith(
            ("ARG ACTIONLINT_VERSION=", "ARG ACTIONLINT_COMMIT=", "ARG ZIZMOR_VERSION=")
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
