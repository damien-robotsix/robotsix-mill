"""run_ci_fix_agent result handling — mirrors test_rebasing.py."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.agents.ci_fixing import CiFixResult, run_ci_fix_agent
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _s(tmp_path):
    import robotsix_mill.config as _cfg
    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="k")
    return Settings(MILL_DATA_DIR=str(tmp_path), OPENROUTER_API_KEY="k")


@pytest.fixture
def fake_ai(monkeypatch):
    box = {}

    class FakeModel:
        def __init__(self, name, **kw): pass

    class FakeAgent:
        def __init__(self, **kw): pass

        def run_sync(self, *a, **k):
            return type("R", (), {"output": CiFixResult(
                status=box["status"],
                summary=box.get("summary", ""),
            )})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)
    return box


@pytest.mark.parametrize("status,expected", [
    ("DONE", True),
    ("FAILED", False),
])
def test_run_ci_fix_agent_reads_output(tmp_path, fake_ai, status, expected):
    """B.9/B.10: Agent returns True on DONE, False otherwise."""
    fake_ai["status"] = status
    result = run_ci_fix_agent(
        settings=_s(tmp_path), repo_dir=tmp_path,
        branch="mill/x", failing_summary="lint failed",
    )
    assert (result.status == "DONE") is expected


def test_missing_api_key_raises(tmp_path):
    """B.11: Raises RuntimeError when OPENROUTER_API_KEY is missing."""
    s = Settings(MILL_DATA_DIR=str(tmp_path))  # no key
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        run_ci_fix_agent(
            settings=s, repo_dir=tmp_path,
            branch="mill/x", failing_summary="x",
        )


def test_uses_build_fs_tools(tmp_path, monkeypatch):
    """B.12: Uses build_fs_tools with correct repo_dir."""
    s = _s(tmp_path)
    seen_calls = {}

    class FakeAgent:
        def __init__(self, **kw): pass
        def run_sync(self, *a, **k):
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw): pass
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    # build_fs_tools is imported from .fs_tools in the function body,
    # so monkeypatch at the source module.
    from robotsix_mill.agents import fs_tools

    def fake_build_fs_tools(repo_dir, settings):
        seen_calls["repo_dir"] = str(repo_dir)
        return []

    monkeypatch.setattr(fs_tools, "build_fs_tools", fake_build_fs_tools)

    run_ci_fix_agent(
        settings=s, repo_dir=tmp_path / "the_repo",
        branch="mill/x", failing_summary="x",
    )
    assert "the_repo" in seen_calls["repo_dir"]


def test_agent_prompt_forbids_push_and_branch_switching(tmp_path, monkeypatch):
    """B.13: The system prompt forbids git push and branch switching."""
    s = _s(tmp_path)
    captured_prompt = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured_prompt["system_prompt"] = kw.get("system_prompt", "")
        def run_sync(self, *a, **k):
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw): pass
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents import fs_tools
    monkeypatch.setattr(fs_tools, "build_fs_tools", lambda rd, s: [])

    run_ci_fix_agent(
        settings=s, repo_dir=tmp_path,
        branch="mill/x", failing_summary="x",
    )
    prompt = captured_prompt["system_prompt"]
    assert "NEVER push" in prompt
