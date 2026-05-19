"""The universal report_issue tool: every agent can file a draft
ticket about a system issue it hit, dedup-guarded against loop spam."""

from robotsix_mill.agents.report_issue import make_report_issue_tool
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def test_files_a_draft_with_agent_source(settings):
    tool = make_report_issue_tool(settings)
    out = tool("rebase agent lacks a force-with-lease option", "details", "missing-tool")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    tickets = svc.list()
    assert len(tickets) == 1
    t = tickets[0]
    assert t.source == "agent"
    assert t.state is State.DRAFT
    assert "category: missing-tool" in svc.workspace(t).read_description()


def test_dedups_while_non_terminal(settings):
    tool = make_report_issue_tool(settings)
    a = tool("missing tool X", "b")
    b = tool("Missing Tool X", "b again")  # case-insensitive same title
    assert a.startswith("report_issue: filed draft ")
    assert "already filed as" in b
    assert len(TicketService(settings).list()) == 1


def test_reallowed_after_resolved(settings):
    tool = make_report_issue_tool(settings)
    tool("flaky thing")
    svc = TicketService(settings)
    t = svc.list()[0]
    # Drive it terminal (draft -> closed is a valid edge).
    svc.transition(t.id, State.CLOSED)
    out = tool("flaky thing")  # same title, prior is closed → allowed
    assert out.startswith("report_issue: filed draft ")
    assert len(svc.list()) == 2


def test_empty_title_rejected(settings):
    tool = make_report_issue_tool(settings)
    assert "non-empty title" in tool("  ", "body")
    assert TicketService(settings).list() == []


def test_unknown_category_coerced_to_other(settings):
    tool = make_report_issue_tool(settings)
    tool("weird thing", "b", "bogus-category")
    svc = TicketService(settings)
    assert "category: other" in svc.workspace(svc.list()[0]).read_description()


def test_never_raises_on_failure(settings, monkeypatch):
    tool = make_report_issue_tool(settings)
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.create",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    out = tool("something", "b")
    assert out.startswith("report_issue: could not file ticket")


def test_build_agent_attaches_report_issue_by_default(settings, monkeypatch):
    """Every agent built via build_agent gets report_issue without the
    caller asking for it (build_agent does lazy imports, so patch at
    the source modules)."""
    captured = {}

    class _FakeAgent:
        def __init__(self, *, model, system_prompt, output_type, tools):
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
    settings.openrouter_api_key = "k"

    from robotsix_mill.agents.base import build_agent

    build_agent(settings, system_prompt="x", tools=[])
    names = {getattr(t, "__name__", "") for t in captured["tools"]}
    assert "report_issue" in names
