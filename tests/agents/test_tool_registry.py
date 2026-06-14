"""Tests for ToolRegistry — the system-wide capability catalog."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents.tool_registry import ToolInfo, ToolRegistry
from robotsix_mill.agents.coordinating import ImplementResult
from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.config import Settings, Secrets, _reset_secrets


# ── helpers ──────────────────────────────────────────────────────────


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
    # Mirror openrouter_api_key into Secrets so get_secrets() works
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    return Settings(**env)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Each test starts with a clean ToolRegistry."""
    ToolRegistry._tools.clear()
    yield
    ToolRegistry._tools.clear()


# ── ToolRegistry tests ────────────────────────────────────────────────


def test_tool_registry_register_and_list():
    """Register a few ToolInfo objects, assert correct count, sort order,
    and content."""
    ToolRegistry.register(
        ToolInfo(
            name="run_command",
            description="Run a shell command.",
            category="shell",
            parameters={"command": "str"},
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="Read a file.",
            category="fs",
            parameters={"path": "str"},
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="explore",
            description="Explore the repo.",
            category="exploration",
            parameters={"question": "str"},
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="write_file",
            description="Write a file.",
            category="fs",
            parameters={"path": "str", "content": "str"},
        )
    )

    tools = ToolRegistry.list_tools()
    assert len(tools) == 4

    # Sort order: category then name.  Expected: fs (read_file,
    # write_file), shell (run_command), exploration (explore).
    names = [t.name for t in tools]
    assert names == ["read_file", "write_file", "run_command", "explore"]

    # Spot-check content
    assert tools[0].category == "fs"
    assert tools[0].description == "Read a file."
    assert tools[2].category == "shell"
    assert tools[3].category == "exploration"


def test_tool_registry_deduplicates_by_name():
    """Register two ToolInfo with the same name, assert only the last
    one survives."""
    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="First registration.",
            category="fs",
            parameters={},
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="Second registration wins.",
            category="fs",
            parameters={"path": "str"},
        )
    )

    tools = ToolRegistry.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "read_file"
    assert tools[0].description == "Second registration wins."
    assert tools[0].parameters == {"path": "str"}


def test_all_tools_registered(tmp_path, monkeypatch):
    """Construct an agent via build_agent() with web_knowledge=True and
    a full tool set (mimicking the coordinator's tool assembly), then
    assert that ToolRegistry.list_tools() contains exactly the expected
    set of tool names."""
    s = _settings(tmp_path)

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in (kw.get("tools") or []))

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": ImplementResult(summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents.explore import make_explore_tool
    from robotsix_mill.agents.fs_tools import build_fs_tools
    from robotsix_mill.agents.base import build_agent

    # Build the agent the same way the coordinator does — this triggers
    # all the tool registrations as a side effect.
    fs = build_fs_tools(tmp_path, s)
    fs_tools = [
        t
        for t in fs
        if t.__name__
        in (
            "read_file",
            "write_file",
            "list_dir",
            "edit_file",
            "delete_file",
            "run_command",
        )
    ]
    _agent = build_agent(
        s,
        system_prompt="test prompt",
        output_type=str,
        tools=[
            make_explore_tool(s, tmp_path),
            *fs_tools,
        ],
        web_knowledge=True,
        name="implement",
    )
    _agent.close()

    registered = {t.name for t in ToolRegistry.list_tools()}
    expected = {
        "ask_user",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "list_dir",
        "list_threads",
        "run_command",
        "explore",
        "ask_web_knowledge",
        "report_issue",
        "reply_to_thread",
        "close_thread",
    }
    assert registered == expected


def test_compose_prompt_does_not_inject_tool_table(tmp_path):
    """``compose_prompt`` no longer appends a prose tool table —
    pydantic-ai forwards the structured ``tools`` array on its own.
    Registering tools must not leak any ``## Available tools`` section
    back into the system prompt."""
    from robotsix_mill.agents.base import compose_prompt

    s = _settings(tmp_path)

    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="Read a file.",
            category="fs",
            parameters={"path": "str"},
        )
    )

    result = compose_prompt(s, "test prompt")
    assert "## Available tools" not in result
    assert "| read_file |" not in result
    # Body of the system prompt is preserved verbatim.
    assert result.strip() == "test prompt"
