"""Tests for the meta repo-triage agent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robotsix_mill.agents import meta_triage
from robotsix_mill.agents.meta_triage import RequiredReposResult
from robotsix_mill.config import RepoConfig, ReposRegistry


def _reg(*repo_ids_with_url):
    return ReposRegistry(
        repos={
            rid: RepoConfig(
                repo_id=rid,
                board_id=rid,
                langfuse_project_name=f"p-{rid}",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                forge_remote_url=url,
            )
            for rid, url in repo_ids_with_url
        }
    )


def test_prompt_covers_contract():
    p = meta_triage.SYSTEM_PROMPT.lower()
    assert "repo_ids" in p
    assert "registered" in p
    # Must instruct: empty list when unsure (the fallback contract).
    assert "empty" in p


def _patch_agent(monkeypatch, repo_ids):
    """Make required_repos_for's agent return RequiredReposResult(repo_ids)."""
    handle = MagicMock()
    handle.run_sync.return_value = MagicMock(
        output=RequiredReposResult(repo_ids=repo_ids, rationale="r")
    )
    monkeypatch.setattr(meta_triage, "_safe_close", lambda _a: None, raising=False)
    return handle


def test_required_repos_validates_against_registry(monkeypatch):
    reg = _reg(("mill", "https://gh/m.git"), ("auto", "https://gh/a.git"))
    monkeypatch.setattr(meta_triage, "get_repos_config", lambda: reg)
    handle = _patch_agent(monkeypatch, ["mill", "ghost", "auto"])
    with (
        patch(
            "robotsix_mill.agents.base.build_agent_from_definition", return_value=handle
        ),
        patch("robotsix_mill.agents.retry.run_agent", lambda agent, make_run, **k: make_run(agent)),
    ):
        out = meta_triage.required_repos_for(settings=MagicMock(), spec="extract X")
    assert out == ["mill", "auto"]  # "ghost" (unknown) dropped


def test_required_repos_falls_back_to_all_when_empty(monkeypatch):
    reg = _reg(("mill", "https://gh/m.git"), ("auto", "https://gh/a.git"))
    monkeypatch.setattr(meta_triage, "get_repos_config", lambda: reg)
    handle = _patch_agent(monkeypatch, [])  # agent unsure → empty
    with (
        patch(
            "robotsix_mill.agents.base.build_agent_from_definition", return_value=handle
        ),
        patch("robotsix_mill.agents.retry.run_agent", lambda agent, make_run, **k: make_run(agent)),
    ):
        out = meta_triage.required_repos_for(settings=MagicMock(), spec="vague")
    assert out == ["auto", "mill"]  # fallback: all clonable, sorted


def test_required_repos_empty_when_no_clonable_repos(monkeypatch):
    reg = _reg(("noclone", None))  # no forge_remote_url
    monkeypatch.setattr(meta_triage, "get_repos_config", lambda: reg)
    out = meta_triage.required_repos_for(settings=MagicMock(), spec="x")
    assert out == []
