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
    # Corrupt manifest → no entries → empty (caller BLOCKs cleanly).
    assert implemented_repos(ws, Settings(), _ticket()) == []
