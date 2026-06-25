"""Unit tests for stages._implemented_repos.implemented_repos.

Covers the two workspace layouts the review/document stages must
handle: legacy single-repo (``ws.dir/"repo"``) and meta multi-repo
(``ws.dir/"repos/<id>"`` + ``touched_repos.json``). The multi-repo
case is the regression these tests pin: review/document used to
hard-BLOCK every meta ticket with "no repository clone" because they
only knew the single-repo path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from robotsix_mill.config import Settings
from robotsix_mill.core.models import Ticket
from robotsix_mill.stages._implemented_repos import implemented_repos


@dataclass
class _FakeWorkspace:
    dir: Path

    @property
    def artifacts_dir(self) -> Path:
        d = self.dir / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _git_clone(path: Path) -> None:
    """Create a stub clone — only ``.git`` presence is checked."""
    (path / ".git").mkdir(parents=True, exist_ok=True)


def _ticket() -> Ticket:
    return Ticket(id="20260603T000000Z-x-abcd", title="x", branch="")


def test_single_repo_layout(tmp_path):
    ws = _FakeWorkspace(dir=tmp_path)
    _git_clone(tmp_path / "repo")

    repos = implemented_repos(ws, Settings(), _ticket())

    assert len(repos) == 1
    assert repos[0].repo_id == ""
    assert repos[0].repo_dir == tmp_path / "repo"


def test_multi_repo_layout_reads_manifest(tmp_path):
    ws = _FakeWorkspace(dir=tmp_path)
    # Two clones under repos/, plus a manifest whose recorded repo_path
    # is a CONTAINER path that does not exist on this host — the helper
    # must reconstruct from ws.dir, not trust repo_path.
    _git_clone(tmp_path / "repos" / "robotsix-mill")
    _git_clone(tmp_path / "repos" / "robotsix-llmio")
    manifest = [
        {
            "repo_id": "robotsix-mill",
            "branch": "mill/x",
            "repo_path": "/data/meta/workspaces/x/repos/robotsix-mill",
        },
        {
            "repo_id": "robotsix-llmio",
            "branch": "mill/y",
            "repo_path": "/data/meta/workspaces/x/repos/robotsix-llmio",
        },
    ]
    ws.artifacts_dir.joinpath("touched_repos.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    repos = implemented_repos(ws, Settings(), _ticket())

    assert [r.repo_id for r in repos] == ["robotsix-mill", "robotsix-llmio"]
    assert repos[0].repo_dir == tmp_path / "repos" / "robotsix-mill"
    assert repos[0].branch == "mill/x"


def test_manifest_entry_without_clone_is_skipped(tmp_path):
    ws = _FakeWorkspace(dir=tmp_path)
    _git_clone(tmp_path / "repos" / "robotsix-mill")  # only this one cloned
    manifest = [
        {"repo_id": "robotsix-mill", "branch": "mill/x"},
        {"repo_id": "robotsix-llmio", "branch": "mill/y"},  # no clone
    ]
    ws.artifacts_dir.joinpath("touched_repos.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    repos = implemented_repos(ws, Settings(), _ticket())

    assert [r.repo_id for r in repos] == ["robotsix-mill"]


def test_no_clone_returns_empty(tmp_path):
    ws = _FakeWorkspace(dir=tmp_path)
    assert implemented_repos(ws, Settings(), _ticket()) == []


def test_corrupt_manifest_returns_empty(tmp_path):
    ws = _FakeWorkspace(dir=tmp_path)
    ws.artifacts_dir.joinpath("touched_repos.json").write_text(
        "{ not json", encoding="utf-8"
    )
    # Corrupt manifest → no entries → no meta clones → falls through to the
    # single-repo path; with no ws.dir/"repo" either, empty (caller BLOCKs).
    assert implemented_repos(ws, Settings(), _ticket()) == []


def test_stale_manifest_falls_back_to_single_repo(tmp_path):
    """A manifest whose clones are all gone must not block when a single-repo
    clone exists.

    Reproduces the central-deploy lifecycle-API bug: #2 was a meta ticket
    (which wrote ``touched_repos.json``), got retargeted to a single-repo
    board, and implement re-cloned into ``ws.dir/"repo"`` — but the stale
    manifest still pointed at the gone ``ws.dir/"repos"/<id>`` paths, so
    review hard-BLOCKED with "no repository clone" while ignoring the valid
    single-repo clone.
    """
    ws = _FakeWorkspace(dir=tmp_path)
    manifest = [
        {
            "repo_id": "robotsix-agent-comm",
            "branch": "mill/x",
            "repo_path": "/data/meta/workspaces/x/repos/robotsix-agent-comm",
        }
    ]
    ws.artifacts_dir.joinpath("touched_repos.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    _git_clone(tmp_path / "repo")  # the real, retargeted single-repo clone

    repos = implemented_repos(ws, Settings(), _ticket())

    assert len(repos) == 1
    assert repos[0].repo_id == ""
    assert repos[0].repo_dir == tmp_path / "repo"


def test_stale_manifest_no_clone_anywhere_returns_empty(tmp_path):
    """Manifest present, no repos/<id> clones, and no ws.dir/"repo" → []."""
    ws = _FakeWorkspace(dir=tmp_path)
    manifest = [{"repo_id": "x", "branch": "b", "repo_path": "/gone"}]
    ws.artifacts_dir.joinpath("touched_repos.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    assert implemented_repos(ws, Settings(), _ticket()) == []
