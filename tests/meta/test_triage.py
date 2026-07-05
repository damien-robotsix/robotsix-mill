"""Tests for the meta repo-triage agent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robotsix_mill.meta import triage as meta_triage
from robotsix_mill.meta.triage import RequiredReposResult
from robotsix_mill.config import RepoConfig, ReposRegistry


def _reg(*repo_ids_with_url):
    return ReposRegistry(
        repos={
            rid: RepoConfig(
                repo_id=rid,
                
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
        patch(
            "robotsix_mill.agents.retry.run_agent",
            lambda agent, make_run, **k: make_run(agent),
        ),
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
        patch(
            "robotsix_mill.agents.retry.run_agent",
            lambda agent, make_run, **k: make_run(agent),
        ),
    ):
        out = meta_triage.required_repos_for(settings=MagicMock(), spec="vague")
    assert out == ["auto", "mill"]  # fallback: all clonable, sorted


def test_fallback_flag_set_only_on_clone_everything(monkeypatch):
    """The result carries ``fallback=True`` only when triage could not
    match a repo (empty agent output → clone everything). A confident
    match leaves it False so deliver's misroute guard stays off."""
    reg = _reg(("mill", "https://gh/m.git"), ("auto", "https://gh/a.git"))
    monkeypatch.setattr(meta_triage, "get_repos_config", lambda: reg)

    # Confident match → fallback False.
    handle = _patch_agent(monkeypatch, ["mill"])
    with (
        patch(
            "robotsix_mill.agents.base.build_agent_from_definition", return_value=handle
        ),
        patch(
            "robotsix_mill.agents.retry.run_agent",
            lambda agent, make_run, **k: make_run(agent),
        ),
    ):
        matched = meta_triage.required_repos_for(
            settings=MagicMock(), spec="touch mill"
        )
    assert matched == ["mill"]
    assert getattr(matched, "fallback", False) is False

    # No usable ids → clone-everything fallback flagged.
    handle = _patch_agent(monkeypatch, [])
    with (
        patch(
            "robotsix_mill.agents.base.build_agent_from_definition", return_value=handle
        ),
        patch(
            "robotsix_mill.agents.retry.run_agent",
            lambda agent, make_run, **k: make_run(agent),
        ),
    ):
        fell_back = meta_triage.required_repos_for(settings=MagicMock(), spec="vague")
    assert fell_back == ["auto", "mill"]
    assert fell_back.fallback is True


def test_required_repos_empty_when_no_clonable_repos(monkeypatch):
    reg = _reg(("noclone", None))  # no forge_remote_url
    monkeypatch.setattr(meta_triage, "get_repos_config", lambda: reg)
    out = meta_triage.required_repos_for(settings=MagicMock(), spec="x")
    assert out == []


def test_required_repos_uses_dedicated_capable_meta_triage_model(monkeypatch):
    """The routing decision runs at the capable tier (level 3, Claude opus),
    NOT a cheap tier. ``required_repos_for`` lets the meta_triage definition's
    own ``level`` (3) flow through ``load_and_run_agent`` unchanged (no level
    override), so we assert no cheap-tier override is forced here."""
    reg = _reg(("mill", "https://gh/m.git"))
    monkeypatch.setattr(meta_triage, "get_repos_config", lambda: reg)
    captured: dict = {}

    def _fake_run(*, settings, definition_name, tools, prompt, what, level=None, **kw):
        captured["definition_name"] = definition_name
        captured["level"] = level
        return MagicMock(output=RequiredReposResult(repo_ids=["mill"], rationale="r"))

    monkeypatch.setattr(
        "robotsix_mill.agents.yaml_loader.load_and_run_agent", _fake_run
    )
    meta_triage.required_repos_for(settings=MagicMock(), spec="x")
    # The meta_triage YAML carries level 3 itself; required_repos_for must not
    # override it down to a cheaper tier.
    assert captured["definition_name"] == "pipeline/meta_triage"
    assert captured["level"] is None


def test_meta_triage_model_defaults_to_capable_tier():
    """The meta_triage agent definition must run at the capable tier (level 3,
    Claude opus) so routing isn't done by the weakest tier."""
    from pathlib import Path

    from robotsix_mill.agents.yaml_loader import load_agent_definition

    defn = load_agent_definition(
        Path(__file__).parent.parent.parent
        / "agent_definitions"
        / "pipeline"
        / "meta_triage.yaml"
    )
    assert defn.level == 3
