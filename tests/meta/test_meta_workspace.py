"""Tests for build_meta_workspace (multi-repo workspace for meta tickets)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from robotsix_mill.meta import workspace as meta_workspace
from robotsix_mill.config import RepoConfig, ReposRegistry


def _reg(*pairs):
    return ReposRegistry(
        repos={
            rid: RepoConfig(
                repo_id=rid,
                
                langfuse_project_name=f"p-{rid}",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                forge_remote_url=url,
            )
            for rid, url in pairs
        }
    )


def test_clones_only_triaged_repos(tmp_path, monkeypatch):
    reg = _reg(
        ("mill", "https://gh/m.git"),
        ("auto", "https://gh/a.git"),
        ("llmio", "https://gh/l.git"),
    )
    monkeypatch.setattr(meta_workspace, "get_repos_config", lambda: reg)
    monkeypatch.setattr(
        meta_workspace, "github_token", lambda settings, repo_config: "tok"
    )

    cloned: list[tuple[str, Path]] = []

    def fake_clone(url, dest, branch, token):
        cloned.append((url, dest))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir()

    monkeypatch.setattr(meta_workspace.git_ops, "clone", fake_clone)

    ws = SimpleNamespace(dir=tmp_path)
    repo_dir, extra_roots = meta_workspace.build_meta_workspace(
        MagicMock(forge_target_branch="main"), ws, ["mill", "llmio"]
    )

    assert [u for u, _ in cloned] == ["https://gh/m.git", "https://gh/l.git"]
    assert repo_dir == tmp_path / "repos" / "mill"
    assert extra_roots == [tmp_path / "repos" / "mill", tmp_path / "repos" / "llmio"]
    assert (tmp_path / "repos" / "auto").exists() is False  # not triaged


def test_skips_unknown_or_unclonable_repo(tmp_path, monkeypatch):
    reg = _reg(("mill", "https://gh/m.git"), ("noclone", None))
    monkeypatch.setattr(meta_workspace, "get_repos_config", lambda: reg)
    monkeypatch.setattr(
        meta_workspace, "github_token", lambda settings, repo_config: None
    )

    def fake_clone(url, dest, branch, token):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir()

    monkeypatch.setattr(meta_workspace.git_ops, "clone", fake_clone)

    ws = SimpleNamespace(dir=tmp_path)
    repo_dir, extra_roots = meta_workspace.build_meta_workspace(
        MagicMock(forge_target_branch="main"), ws, ["mill", "noclone", "ghost"]
    )
    assert repo_dir == tmp_path / "repos" / "mill"
    assert extra_roots == [tmp_path / "repos" / "mill"]


def test_returns_none_when_nothing_cloned(tmp_path, monkeypatch):
    reg = _reg(("mill", "https://gh/m.git"))
    monkeypatch.setattr(meta_workspace, "get_repos_config", lambda: reg)
    out = meta_workspace.build_meta_workspace(
        MagicMock(forge_target_branch="main"), SimpleNamespace(dir=tmp_path), []
    )
    assert out == (None, [])


def test_triaged_blocks_when_required_repo_missing(tmp_path, monkeypatch):
    """A confident (non-fallback) meta triage must clone EVERY named repo.
    If one is missing (e.g. a private repo without creds), the ticket
    BLOCKs with an actionable note rather than handing the implement agent
    a partial workspace that triggers a spurious clarifying question."""
    from robotsix_mill.core.states import State
    import robotsix_mill.meta.triage as mt

    ws = SimpleNamespace(dir=tmp_path, artifacts_dir=tmp_path)
    only_mill = tmp_path / "repos" / "mill"

    monkeypatch.setattr(
        mt, "required_repos_for", lambda *, settings, spec: ["mill", "priv"]
    )
    # build_meta_workspace cloned only "mill" — "priv" failed (best-effort skip)
    monkeypatch.setattr(
        meta_workspace,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (only_mill, [only_mill]),
    )
    comments: list[str] = []
    ctx = SimpleNamespace(
        settings=MagicMock(),
        service=SimpleNamespace(
            add_comment=lambda tid, body, author: comments.append(body)
        ),
    )
    ticket = SimpleNamespace(id="t1")

    repo_dir, extra_roots, outcome = meta_workspace.build_triaged_meta_workspace(
        ctx, ticket, ws, "spec", author="implement"
    )

    assert repo_dir is None and extra_roots is None
    assert outcome is not None and outcome.next_state is State.BLOCKED
    assert "priv" in comments[0]


def test_triaged_proceeds_when_all_required_repos_present(tmp_path, monkeypatch):
    """All confidently-triaged repos cloned → proceed (no block)."""
    import robotsix_mill.meta.triage as mt

    ws = SimpleNamespace(dir=tmp_path, artifacts_dir=tmp_path)
    mill = tmp_path / "repos" / "mill"
    priv = tmp_path / "repos" / "priv"

    monkeypatch.setattr(
        mt, "required_repos_for", lambda *, settings, spec: ["mill", "priv"]
    )
    monkeypatch.setattr(
        meta_workspace,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (mill, [mill, priv]),
    )
    ctx = SimpleNamespace(
        settings=MagicMock(),
        service=SimpleNamespace(add_comment=lambda *a, **k: None),
    )
    ticket = SimpleNamespace(id="t1")

    repo_dir, extra_roots, outcome = meta_workspace.build_triaged_meta_workspace(
        ctx, ticket, ws, "spec", author="implement"
    )
    assert outcome is None
    assert repo_dir == mill and extra_roots == [mill, priv]


def test_wipes_stale_clone_before_recloning(tmp_path, monkeypatch):
    reg = _reg(("mill", "https://gh/m.git"))
    monkeypatch.setattr(meta_workspace, "get_repos_config", lambda: reg)
    monkeypatch.setattr(
        meta_workspace, "github_token", lambda settings, repo_config: "tok"
    )
    dest = tmp_path / "repos" / "mill"
    dest.mkdir(parents=True)
    (dest / "stale.txt").write_text("old run")

    def fake_clone(url, d, branch, token):
        d.mkdir(parents=True, exist_ok=True)
        (d / ".git").mkdir()

    monkeypatch.setattr(meta_workspace.git_ops, "clone", fake_clone)
    meta_workspace.build_meta_workspace(
        MagicMock(forge_target_branch="main"), SimpleNamespace(dir=tmp_path), ["mill"]
    )
    assert not (dest / "stale.txt").exists()  # wiped before fresh clone
