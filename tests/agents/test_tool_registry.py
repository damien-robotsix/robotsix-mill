"""Tests for ToolRegistry — the system-wide capability catalog."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents.tool_registry import ToolInfo, ToolRegistry
from robotsix_mill.agents.coordinating import ImplementResult
from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.config import Settings


# ── helpers ──────────────────────────────────────────────────────────

def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
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
    ToolRegistry.register(ToolInfo(
        name="run_command", description="Run a shell command.",
        category="shell", parameters={"command": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="read_file", description="Read a file.",
        category="fs", parameters={"path": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="explore", description="Explore the repo.",
        category="exploration", parameters={"question": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="write_file", description="Write a file.",
        category="fs", parameters={"path": "str", "content": "str"},
    ))

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


def test_tool_registry_describe_for_prompt():
    """Register tools, call describe_for_prompt(), assert the returned
    string contains a Markdown table with tool names, categories,
    descriptions, category grouping headers, and the strategic guidance
    footer."""
    ToolRegistry.register(ToolInfo(
        name="read_file", description="Return the text content of a file.",
        category="fs", parameters={"path": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="explore", description="Ask a sub-agent a complex question.",
        category="exploration", parameters={"question": "str"},
    ))

    out = ToolRegistry.describe_for_prompt()

    # Must be a Markdown table
    assert "## Available tools" in out
    assert "| Tool | Category | Description |" in out
    assert "|------|----------|-------------|" in out

    # Category grouping headers
    assert "### fs" in out
    assert "### exploration" in out

    # Tool entries
    assert "| read_file | fs | Return the text content of a file. |" in out
    assert "| explore | exploration | Ask a sub-agent a complex question. |" in out

    # Strategic guidance footer
    assert "Prefer direct tools" in out
    assert "Use explore only for complex multi-step questions" in out
    assert "batch related questions into ONE explore call" in out


def test_tool_registry_deduplicates_by_name():
    """Register two ToolInfo with the same name, assert only the last
    one survives."""
    ToolRegistry.register(ToolInfo(
        name="read_file", description="First registration.",
        category="fs", parameters={},
    ))
    ToolRegistry.register(ToolInfo(
        name="read_file", description="Second registration wins.",
        category="fs", parameters={"path": "str"},
    ))

    tools = ToolRegistry.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "read_file"
    assert tools[0].description == "Second registration wins."
    assert tools[0].parameters == {"path": "str"}


def test_tool_registry_empty_describe():
    """Call describe_for_prompt() on an empty registry — asserts it
    returns a sensible message, not an empty table."""
    out = ToolRegistry.describe_for_prompt()
    assert "No tools have been registered yet" in out
    assert "tool registry is empty" in out


def test_describe_for_prompt_filters_by_tool_names():
    """AC1: Register read_file, run_command, report_issue. Call with
    tool_names={"report_issue"}. Assert output contains report_issue
    but NOT read_file or run_command."""
    ToolRegistry.register(ToolInfo(
        name="read_file", description="Read a file.",
        category="fs", parameters={"path": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="run_command", description="Run a shell command.",
        category="shell", parameters={"command": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="report_issue", description="File a draft.",
        category="reporting", parameters={"title": "str"},
    ))

    out = ToolRegistry.describe_for_prompt(tool_names={"report_issue"})

    # Table rows: report_issue appears as a tool, read_file/run_command do not
    assert "| report_issue |" in out
    assert "| read_file |" not in out
    assert "| run_command |" not in out


def test_all_tools_registered(tmp_path, monkeypatch):
    """Construct an agent via build_agent() with web=True and a full
    tool set (mimicking the coordinator's tool assembly), then assert
    that ToolRegistry.list_tools() contains exactly the expected set of
    tool names."""
    s = _settings(tmp_path)

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(
                t.__name__ for t in (kw.get("tools") or [])
            )

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": ImplementResult(summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents import coordinating
    from robotsix_mill.agents.explore import make_explore_tool
    from robotsix_mill.agents.fs_tools import build_fs_tools
    from robotsix_mill.agents.base import build_agent

    # Build the agent the same way the coordinator does — this triggers
    # all the tool registrations as a side effect.
    fs = build_fs_tools(tmp_path, s)
    fs_tools = [
        t for t in fs if t.__name__ in
        ("read_file", "write_file", "list_dir", "edit_file", "delete_file", "run_command")
    ]
    _agent = build_agent(
        s,
        system_prompt="test prompt",
        output_type=str,
        tools=[
            make_explore_tool(s, tmp_path),
            *fs_tools,
            make_run_tests_tool := coordinating.make_run_tests_tool(s, tmp_path),
        ],
        web=True,
        name="implement",
    )
    _agent.close()

    registered = {t.name for t in ToolRegistry.list_tools()}
    expected = {
        "read_file", "write_file", "edit_file", "delete_file",
        "list_dir", "run_command", "explore", "run_tests",
        "web_research", "report_issue",
    }
    assert registered == expected


def test_compose_prompt_includes_capability_table(tmp_path):
    """Call _compose_prompt after registering at least one tool, assert
    the result starts with the original prompt and contains the
    capability table. Also test that tool_names filters correctly."""
    from robotsix_mill.agents.base import compose_prompt

    s = _settings(tmp_path)

    ToolRegistry.register(ToolInfo(
        name="read_file", description="Read a file.",
        category="fs", parameters={"path": "str"},
    ))

    result = compose_prompt(s, "test prompt")
    assert result.startswith("test prompt")
    assert "## Available tools" in result
    assert "| read_file | fs | Read a file. |" in result
    assert "Prefer direct tools" in result

    # AC2: tool_names filter
    ToolRegistry.register(ToolInfo(
        name="report_issue", description="File a draft.",
        category="reporting", parameters={"title": "str"},
    ))
    result2 = compose_prompt(
        s, "test prompt", tool_names={"report_issue"}
    )
    assert "| report_issue |" in result2
    assert "| read_file |" not in result2
