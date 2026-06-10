"""Tests for ``Workspace.screenshots_dir`` and ``list_screenshots``."""

from __future__ import annotations

from pathlib import Path

from robotsix_mill.core.workspace import Workspace


def test_screenshots_dir_created_lazily(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    d = ws.screenshots_dir
    assert d == ws.dir / "screenshots"
    assert d.is_dir()


def test_screenshots_dir_is_sibling_of_artifacts(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    # Must NOT live under artifacts/ — a refine reset wipes artifacts/.
    assert ws.screenshots_dir.parent == ws.dir
    assert ws.screenshots_dir != ws.artifacts_dir


def test_list_screenshots_empty_when_absent(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    assert ws.list_screenshots() == []


def test_list_screenshots_sorted_and_filtered(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    d = ws.screenshots_dir
    (d / "b.png").write_bytes(b"x")
    (d / "a.jpg").write_bytes(b"x")
    (d / "c.webp").write_bytes(b"x")
    (d / "notes.txt").write_text("ignore me")
    (d / "sub").mkdir()  # directories excluded

    names = [p.name for p in ws.list_screenshots()]
    assert names == ["a.jpg", "b.png", "c.webp"]
