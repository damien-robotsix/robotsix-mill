"""Tests for the ``ask_user`` tool — pause a ticket and ask the
operator a clarifying question."""

import pytest

from robotsix_mill.agents.ask_user import make_ask_user_tool
from robotsix_mill.agents.tool_registry import ToolRegistry
from robotsix_mill.core.service import TicketService


def test_writes_comment_with_ask_user_marker(settings, service, monkeypatch):
    """Calling ask_user writes a comment with [ASK_USER] prefix."""
    ticket = service.create("Test ticket", "desc")
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: ticket.id,
    )
    tool = make_ask_user_tool(settings, agent_name="refine")
    result = tool("Should I proceed with approach A or B?")

    assert result == "__ASK_USER_PAUSE__"

    svc = TicketService(settings)
    comments = svc.list_comments(ticket.id)
    assert len(comments) == 1
    comment = comments[0]
    assert comment.body.startswith("[ASK_USER]")
    assert "approach A or B" in comment.body
    assert comment.author == "refine"
    assert comment.parent_id is None


def test_returns_sentinel_on_first_call(settings, service, monkeypatch):
    """First call returns exactly __ASK_USER_PAUSE__."""
    ticket = service.create("Test ticket", "desc")
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: ticket.id,
    )
    tool = make_ask_user_tool(settings, agent_name="test-agent")
    result = tool("What should I do?")
    assert result == "__ASK_USER_PAUSE__"


def test_idempotent_second_call_no_duplicate_comment(settings, service, monkeypatch):
    """Second call returns sentinel without creating a second comment."""
    ticket = service.create("Test ticket", "desc")
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: ticket.id,
    )
    tool = make_ask_user_tool(settings, agent_name="test-agent")
    r1 = tool("Q1")
    r2 = tool("Q2")

    assert r1 == "__ASK_USER_PAUSE__"
    assert r2 == "__ASK_USER_PAUSE__"

    svc = TicketService(settings)
    comments = svc.list_comments(ticket.id)
    assert len(comments) == 1
    assert "Q1" in comments[0].body
    for c in comments:
        assert "Q2" not in c.body


def test_no_active_session_returns_error_string(settings, monkeypatch):
    """When current_session returns None, returns error string."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: None,
    )
    tool = make_ask_user_tool(settings, agent_name="test-agent")
    result = tool("What now?")
    assert result.startswith("Error: no active ticket session")


def test_never_raises_on_service_failure(settings, service, monkeypatch):
    """When TicketService.add_comment raises, the tool catches it."""
    ticket = service.create("Test ticket", "desc")
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: ticket.id,
    )
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.add_comment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    tool = make_ask_user_tool(settings, agent_name="test-agent")
    # Should not raise
    result = tool("Some question")
    assert result.startswith("ask_user: could not post question")


def test_tool_registered_in_registry(settings, service, monkeypatch):
    """The tool self-registers in the ToolRegistry."""
    ticket = service.create("Test ticket", "desc")
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: ticket.id,
    )
    # Clear previous registrations so we don't get stale entries.
    ToolRegistry._tools.pop("ask_user", None)
    make_ask_user_tool(settings, agent_name="test-agent")

    tools = ToolRegistry.list_tools()
    ask_user_infos = [t for t in tools if t.name == "ask_user"]
    assert len(ask_user_infos) == 1
    info = ask_user_infos[0]
    assert info.name == "ask_user"
    assert info.category == "reporting"


def test_build_agent_injects_ask_user_by_default(settings, monkeypatch, secrets_set):
    """build_agent injects ask_user when ask_user=True (the default)."""
    captured = {}

    class _FakeAgent:
        def __init__(self, *, model, system_prompt, output_type, tools, retries):
            captured["tools"] = tools

    monkeypatch.setattr("pydantic_ai.Agent", _FakeAgent)
    monkeypatch.setattr(
        "pydantic_ai.providers.openrouter.OpenRouterProvider",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.openrouter_cost."
        "CostInstrumentedOpenRouterModel",
        lambda *a, **k: object(),
    )
    secrets_set(openrouter_api_key="k")

    from robotsix_mill.agents.base import build_agent

    build_agent(settings, system_prompt="x", tools=[])
    names = {getattr(t, "__name__", "") for t in captured["tools"]}
    assert "ask_user" in names


def test_build_agent_omits_ask_user_when_false(settings, monkeypatch, secrets_set):
    """build_agent(ask_user=False) does not include the ask_user tool."""
    captured = {}

    class _FakeAgent:
        def __init__(self, *, model, system_prompt, output_type, tools, retries):
            captured["tools"] = tools

    monkeypatch.setattr("pydantic_ai.Agent", _FakeAgent)
    monkeypatch.setattr(
        "pydantic_ai.providers.openrouter.OpenRouterProvider",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.openrouter_cost."
        "CostInstrumentedOpenRouterModel",
        lambda *a, **k: object(),
    )
    secrets_set(openrouter_api_key="k")

    from robotsix_mill.agents.base import build_agent

    build_agent(settings, system_prompt="x", tools=[], ask_user=False)
    names = {getattr(t, "__name__", "") for t in captured["tools"]}
    assert "ask_user" not in names
