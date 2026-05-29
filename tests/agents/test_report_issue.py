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


def test_code_quality_category_accepted_and_preserved(settings):
    """The code-quality category is valid and appears in the ticket body."""
    tool = make_report_issue_tool(settings)
    out = tool("Redundant query in user lookup", "found a duplicate DB call", "code-quality")
    assert out.startswith("report_issue: filed draft ")
    svc = TicketService(settings)
    desc = svc.workspace(svc.list()[0]).read_description()
    assert "category: code-quality" in desc
    assert "found a duplicate DB call" in desc


def test_never_raises_on_failure(settings, monkeypatch):
    tool = make_report_issue_tool(settings)
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.create",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    out = tool("something", "b")
    assert out.startswith("report_issue: could not file ticket")


def test_build_agent_attaches_report_issue_by_default(settings, monkeypatch, secrets_set):
    """Every agent built via build_agent gets report_issue without the
    caller asking for it (build_agent does lazy imports, so patch at
    the source modules)."""
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
    assert "report_issue" in names


def test_noop_title_not_filed(settings):
    """A 'nothing to report' self-report is dropped (no ticket), with a
    friendly non-error string — shares the retrospect no-op detector."""
    tool = make_report_issue_tool(settings)
    for t in ("No notable issues - clean run",
              "Nothing to report",
              "Clean ticket, no issues to flag"):
        out = tool(t, "agent had nothing to flag")
        assert "not filed" in out and "no-op" in out
    assert TicketService(settings).list() == []  # zero tickets created


def test_genuine_terse_title_still_filed(settings):
    """A real (terse) issue is still filed — no over-filtering."""
    tool = make_report_issue_tool(settings)
    out = tool("Fix timeout in rebase loop", "details")
    assert out.startswith("report_issue: filed draft ")
    assert len(TicketService(settings).list()) == 1


def test_build_agent_without_report_issue(settings, monkeypatch, secrets_set):
    """build_agent(report_issue=False) omits the report_issue tool."""
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

    build_agent(settings, system_prompt="x", tools=[], report_issue=False)
    names = {getattr(t, "__name__", "") for t in captured["tools"]}
    assert "report_issue" not in names


def test_audit_agent_omits_report_issue(settings, monkeypatch, secrets_set):
    """The audit agent (which emits drafts via structured output) must
    not also get the report_issue tool."""
    captured = {}

    class _FakeAgent:
        def __init__(self, *, model, system_prompt, output_type, tools, name=None, retries=None):
            captured["tools"] = tools
            captured["name"] = name

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
    # Also stub PromptedOutput so we don't need a real model
    monkeypatch.setattr(
        "pydantic_ai.PromptedOutput",
        lambda x: x,
    )
    # Stub call_with_retry to avoid executing the agent
    monkeypatch.setattr(
        "robotsix_mill.agents.retry.call_with_retry",
        lambda fn, *a, **k: None,
    )
    secrets_set(openrouter_api_key="k")

    from robotsix_mill.agents.auditing import run_audit_agent

    # Call without a repo_dir to keep tools list minimal
    try:
        run_audit_agent(settings=settings)
    except Exception:
        pass  # call_with_retry stubbed so we may hit None.output

    names = {getattr(t, "__name__", "") for t in captured["tools"]}
    assert "report_issue" not in names


def test_origin_session_captured_from_current_session(settings, monkeypatch):
    """When current_session() returns a value, the filed ticket gets
    origin_session set."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: "audit-20250101-abc123",
    )
    tool = make_report_issue_tool(settings)
    out = tool("Some issue", "details", "error")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    assert t.origin_session == "audit-20250101-abc123"


def test_origin_session_none_when_no_session(settings):
    """When current_session() returns None, origin_session stays None."""
    # By default _current_session is None (no tracing session in scope).
    tool = make_report_issue_tool(settings)
    out = tool("Another issue", "details", "error")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    assert t.origin_session is None


def test_evidence_persisted_to_artifacts_dir(settings):
    """When evidence is supplied, it's written to artifacts/evidence.txt
    and description.md ends with the pointer line."""
    tool = make_report_issue_tool(settings)
    out = tool(
        "Flaky test failure",
        "test_foo fails intermittently",
        "error",
        evidence="$ pytest tests/test_foo.py\nFAILED tests/test_foo.py::test_case - AssertionError: ...",
    )
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    workspace = svc.workspace(t)
    evidence_path = workspace.artifacts_dir / "evidence.txt"
    assert evidence_path.exists()
    assert "pytest tests/test_foo.py" in evidence_path.read_text(encoding="utf-8")

    desc = workspace.read_description()
    assert "> Raw evidence attached at artifacts/evidence.txt" in desc


def test_no_evidence_creates_no_file_and_no_pointer(settings):
    """When evidence is empty/not passed, no evidence.txt is created
    and description.md has no pointer line."""
    tool = make_report_issue_tool(settings)
    out = tool("Missing tool", "need a force-push tool", "missing-tool")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    workspace = svc.workspace(t)
    evidence_path = workspace.artifacts_dir / "evidence.txt"
    assert not evidence_path.exists()

    desc = workspace.read_description()
    assert "Raw evidence attached at artifacts/evidence.txt" not in desc


def test_evidence_truncated_at_64kb(settings):
    """Evidence longer than 64 KiB is truncated to exactly 65536 bytes
    before writing. The cap was raised from 8 KiB to 64 KiB so CI
    logs / stack traces / longer test diagnostics survive without
    losing signal."""
    tool = make_report_issue_tool(settings)
    chunk = "line "  # 5 bytes
    evidence = chunk * 14000  # 5 * 14000 = 70_000 bytes > 64 KiB
    assert len(evidence.encode("utf-8")) > 65536

    out = tool("Truncation test", "body", "error", evidence=evidence)
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    workspace = svc.workspace(t)
    evidence_path = workspace.artifacts_dir / "evidence.txt"
    written = evidence_path.read_bytes()
    assert len(written) == 65536


def test_evidence_under_8kb_passes_through_after_cap_raise(settings):
    """Sanity: a payload that was over the OLD 8 KiB cap (but under
    the new 64 KiB cap) survives intact — guards against accidentally
    dropping the threshold back."""
    tool = make_report_issue_tool(settings)
    payload = "x" * (16 * 1024)  # 16 KiB
    tool("Mid-size evidence", "body", "error", evidence=payload)
    svc = TicketService(settings)
    t = svc.list()[0]
    workspace = svc.workspace(t)
    written = (workspace.artifacts_dir / "evidence.txt").read_bytes()
    assert len(written) == len(payload)

    # Description must still have the pointer line.
    desc = workspace.read_description()
    assert "> Raw evidence attached at artifacts/evidence.txt" in desc


def test_empty_evidence_whitespace_only_treated_as_no_evidence(settings):
    """Whitespace-only evidence is treated as if no evidence was given."""
    tool = make_report_issue_tool(settings)
    out = tool("Whitespace evidence", "body", "error", evidence="   \n  ")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    workspace = svc.workspace(t)
    evidence_path = workspace.artifacts_dir / "evidence.txt"
    assert not evidence_path.exists()

    desc = workspace.read_description()
    assert "Raw evidence attached at artifacts/evidence.txt" not in desc


def test_agent_name_stamped_in_body(settings):
    """When agent_name is given, the ticket body includes it."""
    tool = make_report_issue_tool(settings, agent_name="run_tests")
    out = tool("Missing read tool", "agent needs a read_file tool", "missing-tool")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    desc = svc.workspace(t).read_description()
    assert "**Reported by the `run_tests` agent** (category: missing-tool)" in desc
    assert "agent needs a read_file tool" in desc


def test_agent_name_none_falls_back_to_generic(settings):
    """When agent_name is None (the default), the generic wording is used."""
    tool = make_report_issue_tool(settings, agent_name=None)
    out = tool("Generic agent report", "some body", "error")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    desc = svc.workspace(t).read_description()
    assert "**Reported by an agent** (category: error)" in desc
    assert "`None`" not in desc


def test_agent_name_empty_string_falls_back(settings):
    """When agent_name is an empty string, the generic wording is used."""
    tool = make_report_issue_tool(settings, agent_name="")
    out = tool("Empty name agent", "body", "workflow-improvement")
    assert out.startswith("report_issue: filed draft ")

    svc = TicketService(settings)
    t = svc.list()[0]
    desc = svc.workspace(t).read_description()
    assert "**Reported by an agent** (category: workflow-improvement)" in desc
