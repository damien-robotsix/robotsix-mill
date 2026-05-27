"""The domain expert consultation sub-agent."""

from pathlib import Path

from robotsix_mill.agents import consult_expert
from robotsix_mill.agents.consult_expert import (
    make_consult_expert_tool,
    run_consult_expert,
)
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg
        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    return Settings(**env)


def test_no_key_degrades_not_raises(tmp_path):
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = run_consult_expert(
        settings=s, repo_dir=tmp_path, domain="python-backend",
        question="where is X?",
    )
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_missing_repo_degrades_not_raises(tmp_path):
    missing = tmp_path / "nonexistent"
    s = _settings(tmp_path, OPENROUTER_API_KEY="valid-key")
    out = run_consult_expert(
        settings=s, repo_dir=missing, domain="python-backend",
        question="where is X?",
    )
    assert "unavailable" in out


def test_tool_delegates_to_seam(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    def fake(*, settings, repo_dir, domain, question):
        seen["domain"] = domain
        seen["question"] = question
        seen["dir"] = repo_dir
        return f"ANSWER: {domain} -> {question}"

    monkeypatch.setattr(consult_expert, "run_consult_expert", fake)
    tool = make_consult_expert_tool(s, tmp_path)
    result = tool("python-backend", "where is the Settings class defined?")
    assert result == "ANSWER: python-backend -> where is the Settings class defined?"
    assert seen["domain"] == "python-backend"
    assert seen["question"] == "where is the Settings class defined?"
    assert seen["dir"] == tmp_path


def test_missing_expert_definition_degrades_not_raises(tmp_path, monkeypatch):
    """When the expert definition YAML file doesn't exist,
    run_consult_expert returns a failure string without raising."""
    from robotsix_mill.agents import expert_manager
    from robotsix_mill.agents.expert_loader import (
        ExpertMemoryConfig, ExpertDefinition,
    )

    s = _settings(tmp_path, OPENROUTER_API_KEY="valid-key")

    # Patch load_definitions to return a known dict that lacks
    # 'nonexistent' — mimics a missing YAML file.
    def fake_load_defs(self, definitions_dir=None):
        return {
            "python-backend": ExpertDefinition(
                domain="python-backend",
                module_paths=["src/**/*.py"],
                system_prompt="You are a Python expert.",
                model="",
                memory=ExpertMemoryConfig(max_memory_chars=8000),
                tools=["explore", "read_file", "list_dir"],
            ),
        }

    monkeypatch.setattr(
        expert_manager.ExpertManager, "load_definitions", fake_load_defs,
    )

    out = run_consult_expert(
        settings=s, repo_dir=tmp_path, domain="nonexistent",
        question="where is X?",
    )
    assert "nonexistent" in out
    assert "no expert definition found" in out


def test_expert_agent_read_only_tools(tmp_path, monkeypatch):
    """The expert agent built inside run_consult_expert has ONLY
    explore, read_file, list_dir — no edit_file, write_file,
    run_command, delete_file."""
    from robotsix_mill.agents import expert_manager
    from robotsix_mill.agents.expert_loader import (
        ExpertMemoryConfig, ExpertDefinition,
    )

    s = _settings(
        tmp_path, OPENROUTER_API_KEY="k",
        MILL_MODEL="coordinator/big", MILL_CONSULT_REQUEST_LIMIT="5",
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in kw.get("tools", []))
            cap["name"] = kw.get("name")
            cap["output_type"] = kw.get("output_type")

        def run_sync(self, prompt, **kw):
            class R:
                output = "expert answer"
            return R()

    def fake_load_defs(self, definitions_dir=None):
        return {
            "python-backend": ExpertDefinition(
                domain="python-backend",
                module_paths=["src/**/*.py"],
                system_prompt="You are a Python expert.",
                model="",
                memory=ExpertMemoryConfig(max_memory_chars=8000),
                tools=["explore", "read_file", "list_dir"],
            ),
        }

    monkeypatch.setattr(
        expert_manager.ExpertManager, "load_definitions", fake_load_defs,
    )
    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)
    # Prevent the fs_tools build from trying to access a real repo
    from robotsix_mill.agents import fs_tools as ft
    monkeypatch.setattr(ft, "build_fs_tools", lambda root, settings, **kw: [])
    # Prevent timeout_http_client from opening a real client
    from robotsix_mill.agents import base as bmod
    monkeypatch.setattr(bmod, "timeout_http_client", lambda s: None)

    out = run_consult_expert(
        settings=s, repo_dir=tmp_path, domain="python-backend",
        question="where is X?",
    )
    assert out == "expert answer"
    assert cap["name"] == "consult:python-backend"
    # Only read-only tools — no mutation tools.
    for banned in ("edit_file", "write_file", "run_command", "delete_file"):
        assert banned not in cap["tools"], f"{banned} should not be in expert tools"
    assert cap["output_type"] == str


def test_tool_registry_registration(tmp_path):
    """make_consult_expert_tool registers the tool in ToolRegistry."""
    from robotsix_mill.agents.tool_registry import ToolRegistry

    s = _settings(tmp_path)
    tool = make_consult_expert_tool(s, tmp_path)
    tools = ToolRegistry.list_tools()
    consult = [t for t in tools if t.name == "consult_expert"]
    assert len(consult) == 1
    assert consult[0].category == "exploration"
    assert "domain" in consult[0].parameters
    assert "question" in consult[0].parameters
