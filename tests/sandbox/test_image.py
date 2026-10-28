"""Sandbox image resolution: digest pin to the running mill, hourly pulls."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

from robotsix_mill import sandbox
from robotsix_mill.config import Settings
from robotsix_mill.sandbox import _image, _inventory

MILL = "ghcr.io/damien-robotsix/robotsix-mill"
PINNED = f"{MILL}@sha256:984703b8"
RUNNING_ID = "sha256:running"


class FakeDocker:
    """Fake ``subprocess.run`` answering the docker calls the resolver makes."""

    def __init__(self, config_image: str = PINNED, tag_id: str = RUNNING_ID):
        self.config_image = config_image
        self.tag_id = tag_id
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        text = kwargs.get("text", False)
        out = ""
        if argv[:2] == ["docker", "inspect"]:
            out = f"{self.config_image} {RUNNING_ID}\n"
        elif argv[:3] == ["docker", "image", "inspect"]:
            out = f"{self.tag_id}\n"
        elif argv[:2] == ["docker", "run"] and "--pull=never" in argv:
            out = "+uv\n+gh\n+git\n+actionlint\n+zizmor\n+uvx\n"
        elif argv[:2] == ["docker", "run"]:
            out = "ok"
        if not text:
            return subprocess.CompletedProcess(argv, 0, out.encode(), b"")
        return subprocess.CompletedProcess(argv, 0, out, "")

    def pulls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["docker", "pull"]]


def test_uninitialised_resolver_is_inert(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(_image.subprocess, "run", fake)
    assert _image.resolve_sandbox_image(f"{MILL}:main") == f"{MILL}:main"
    assert fake.calls == []


def test_create_uses_running_digest_when_repositories_match(
    tmp_path, monkeypatch, caplog
):
    fake = FakeDocker()
    monkeypatch.setattr(_image.subprocess, "run", fake)
    monkeypatch.setattr(sandbox, "_repo_mount", lambda repo_dir, settings: [])
    s = Settings(
        data_dir=str(tmp_path), sandbox_image=f"{MILL}:main", sandbox_proxy_url=""
    )

    with caplog.at_level(logging.INFO, logger="robotsix_mill.sandbox"):
        assert _image.init_sandbox_image(s) == PINNED
    assert f"sandbox image resolved to {PINNED}" in caplog.text

    rc, _ = sandbox.run("true", repo_dir=tmp_path, settings=s)
    assert rc == 0
    create = [c for c in fake.calls if c[:2] == ["docker", "run"]][-1]
    assert create[create.index("--entrypoint") + 2] == PINNED
    assert fake.pulls() == []


def test_falls_back_to_image_id_when_config_image_is_a_tag(monkeypatch):
    monkeypatch.setattr(
        _image.subprocess, "run", FakeDocker(config_image=f"{MILL}:main")
    )
    s = SimpleNamespace(sandbox_image=f"{MILL}:main")
    assert _image.init_sandbox_image(s) == RUNNING_ID


def test_distinct_image_is_pulled_at_most_once_per_hour(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(_image.subprocess, "run", fake)
    now = [1000.0]
    monkeypatch.setattr(_image.time, "monotonic", lambda: now[0])
    image = "ghcr.io/damien-robotsix/robotsix-mill-sandbox:main"

    assert _image.init_sandbox_image(SimpleNamespace(sandbox_image=image)) == image
    assert fake.pulls() == [["docker", "pull", "--quiet", image]]

    now[0] += 1800
    assert _image.resolve_sandbox_image(image) == image
    assert len(fake.pulls()) == 1

    now[0] += 1801
    assert _image.resolve_sandbox_image(image) == image
    assert len(fake.pulls()) == 2


def test_inventory_names_stale_tag_digest(monkeypatch, caplog):
    fake = FakeDocker(tag_id="sha256:stale")
    monkeypatch.setattr(_image.subprocess, "run", fake)
    s = SimpleNamespace(sandbox_image=f"{MILL}:main")
    _image.init_sandbox_image(s)

    with caplog.at_level(logging.INFO, logger="robotsix_mill.sandbox"):
        _inventory.log_sandbox_tool_inventory(s)
    assert "sandbox tag digest sha256:stale != running digest sha256:running" in (
        caplog.text
    )
    probe = [c for c in fake.calls if "--pull=never" in c][-1]
    assert PINNED in probe
