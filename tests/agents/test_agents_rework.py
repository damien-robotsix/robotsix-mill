"""The implement agent + test sub-agent: the main agent reads/edits
itself, with a concise `explore` scout and a distilling `run_tests`
sub-agent (no implement sub-agent, no deep layer)."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents import coordinating, testing
from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.agents.coordinating import ImplementResult, ValidationResult
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
    # Populate Secrets so get_secrets() returns matching values
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg
    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key=env.get("OPENROUTER_API_KEY", "k"))
    return Settings(**env)


@pytest.fixture
def fake_ai(monkeypatch):
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(
                t.__name__ for t in (kw.get("tools") or [])
            )
            cap["name"] = kw.get("name")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["limit"] = getattr(usage_limits, "request_limit", None)
            return type("R", (), {"output": ImplementResult(summary="did it")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)
    return cap


def test_implement_agent_reads_and_edits_itself(tmp_path, fake_ai):
    """The main agent uses MILL_MODEL and gets explore (scout) + its
    OWN fs tools (incl. run_command for focused diagnosis) +
    web_research. There is NO run_tests tool — the implement stage owns
    the test→retry→escalate loop and runs the suite itself."""
    s = _settings(
        tmp_path, model="main/cap",
        coordinator_request_limit="9",
    )
    out = coordinating.run_coordinator(
        settings=s, repo_dir=tmp_path, spec="build a thing"
    )
    assert out.summary == "did it"
    assert fake_ai["model"] == "main/cap"
    assert fake_ai["limit"] == 9
    assert fake_ai["tools"] == [
        "ask_user", "close_thread", "consult_expert", "consult_library",
        "delete_file", "edit_file", "explore",
        "list_dir", "read_file", "reply_to_thread", "report_issue",
        "run_command", "spawn_subtask",
        "web_research", "write_file",
    ]
    assert fake_ai["name"] == "implement"


def test_explore_scout_prompt_forbids_whole_files():
    from robotsix_mill.agents.explore import _SYSTEM_PROMPT

    assert "NEVER paste whole files" in _SYSTEM_PROMPT
    assert "FILE:" not in _SYSTEM_PROMPT  # the old dump-file directive is gone

    # scope-discipline guardrails (ticket: explore-scope-guardrails)
    assert "at most 5 files" in _SYSTEM_PROMPT.lower()
    assert "do not trace full call chains" in _SYSTEM_PROMPT.lower()


def test_test_agent_pass(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    monkeypatch.setattr(
        sandbox, "run", lambda cmd, *, repo_dir, settings,
        epic_workspace_path=None: (0, "ok")
    )
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True and "passed" in fb


def test_test_agent_fail_distills_via_cheap_model(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = _settings(
        tmp_path, test_command="pytest", test_model="test/cheap",
    )
    monkeypatch.setattr(
        sandbox, "run",
        lambda cmd, *, repo_dir, settings,
        epic_workspace_path=None: (1, "E   assert 1 == 2\n" * 50),
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")
            cap["tools"] = sorted(
                t.__name__ for t in (kw.get("tools") or [])
            )

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["got_output"] = "assert 1 == 2" in prompt
            return type("R", (), {"output": "fix the assertion in foo.py"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert fb == "fix the assertion in foo.py"  # distilled, not raw log
    assert cap["model"] == "test/cheap" and cap["got_output"]
    assert cap["name"] == "run_tests"

    # AC4: run_tests agent has read-only diagnostic tools
    assert "read_file" in cap["tools"]
    assert "list_dir" in cap["tools"]
    assert "run_command" in cap["tools"]
    assert "explore" in cap["tools"]
    assert "report_issue" in cap["tools"]
    assert "write_file" not in cap["tools"]
    assert "edit_file" not in cap["tools"]
    assert "delete_file" not in cap["tools"]


def test_test_agent_no_command_is_pass(tmp_path):
    s = _settings(tmp_path, test_command="")
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True


def test_build_agent_forwards_name(tmp_path, monkeypatch):
    """AC1: build_agent(..., name='test-agent') passes name= to Agent."""
    from robotsix_mill.agents import base as base_mod

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": "ok"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    s = _settings(tmp_path)
    base_mod.build_agent(
        s, system_prompt="test", name="test-agent",
    )
    assert cap["name"] == "test-agent"


def test_build_agent_does_not_inject_tool_prose_into_prompt(tmp_path, monkeypatch):
    """The agent's system_prompt is the YAML body verbatim — no prose
    tool list is appended. pydantic-ai forwards each closure's
    signature + docstring as the model API's structured ``tools``
    array; a Markdown copy in the prompt would be pure duplication.

    Replaces the previous AC3 test that asserted owned tools appeared
    in the prompt but unowned ones didn't — the contract changed when
    we deduped the tool surface."""
    from robotsix_mill.agents.base import build_agent
    from robotsix_mill.agents.tool_registry import ToolInfo, ToolRegistry

    s = _settings(tmp_path)

    ToolRegistry.register(ToolInfo(
        name="write_file", description="Write a file.",
        category="fs", parameters={"path": "str", "content": "str"},
    ))

    def dummy_tool():
        """A dummy tool."""
        pass

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["system_prompt"] = kw.get("system_prompt", "")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": "ok"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    agent = build_agent(
        s,
        system_prompt="test prompt",
        tools=[dummy_tool],
    )
    agent.close()

    # The prompt is the YAML body verbatim — no prose tool table.
    assert "## Available tools" not in cap["system_prompt"]
    # No tool names leak into the prompt body.
    assert "dummy_tool" not in cap["system_prompt"]
    assert "write_file" not in cap["system_prompt"]


def test_build_agent_without_name_is_compatible(tmp_path, monkeypatch):
    """AC2: build_agent(...) without name= still works; Agent receives
    no name kwarg (or None)."""
    from robotsix_mill.agents import base as base_mod

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": "ok"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    s = _settings(tmp_path)
    base_mod.build_agent(
        s, system_prompt="test",
    )
    assert cap["name"] is None


def test_audit_agent_tool_set(tmp_path, monkeypatch):
    """AC: audit agent gets explore, list_dir, read_file, run_command,
    and web_research — exactly five tools."""
    from robotsix_mill.agents import auditing

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
            from robotsix_mill.agents.auditing import AuditResult
            return type("R", (), {
                "output": AuditResult(
                    draft_ticket_titles=[], draft_ticket_bodies=[],
                    gap_ids=[], updated_memory="",
                )
            })()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    s = _settings(tmp_path)
    auditing.run_audit_agent(settings=s, repo_dir=tmp_path, memory="")

    assert cap["tools"] == [
        "ask_user", "close_thread", "detect_duplication", "explore", "list_dir",
        "read_file", "read_ticket", "run_command", "web_research",
    ]


def test_validation_result_decide_proceed():
    """A passing gate routes to proceed regardless of iteration count."""
    vr = ValidationResult.decide(
        passed=True, iterations=1, max_iters=8, feedback="",
    )
    assert vr.passed is True
    assert vr.next_action == "proceed"
    assert vr.failure_summary == ""
    assert vr.iterations_used == 1


def test_validation_result_decide_retry():
    """A failing gate with attempts remaining routes to retry and
    carries the diagnosis as failure_summary."""
    vr = ValidationResult.decide(
        passed=False, iterations=1, max_iters=8, feedback="boom in test_x",
    )
    assert vr.passed is False
    assert vr.next_action == "retry"
    assert vr.failure_summary == "boom in test_x"
    assert vr.iterations_used == 1


def test_validation_result_decide_escalate():
    """A failing gate on the last allowed attempt routes to escalate —
    no LLM involvement, the bound is enforced here."""
    vr = ValidationResult.decide(
        passed=False, iterations=3, max_iters=3, feedback="still broken",
    )
    assert vr.next_action == "escalate"
    assert vr.passed is False
    assert vr.failure_summary == "still broken"
    assert vr.iterations_used == 3
