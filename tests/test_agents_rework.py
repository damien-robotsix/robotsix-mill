"""The implement agent + test sub-agent: the main agent reads/edits
itself, with a concise `explore` scout and a distilling `run_tests`
sub-agent (no implement sub-agent, no deep layer)."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents import coordinating, testing
from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.agents.coordinating import ImplementResult
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
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
    OWN fs tools + run_tests + web_research — no implement sub-agent,
    no raw run_command (tests go via the test sub-agent)."""
    s = _settings(
        tmp_path, MILL_MODEL="main/cap",
        MILL_COORDINATOR_REQUEST_LIMIT="9",
    )
    out = coordinating.run_coordinator(
        settings=s, repo_dir=tmp_path, spec="build a thing"
    )
    assert out.summary == "did it"
    assert fake_ai["model"] == "main/cap"
    assert fake_ai["limit"] == 9
    assert fake_ai["tools"] == [
        "delete_file", "edit_file", "explore", "list_dir", "read_file",
        "report_issue", "run_tests", "web_research", "write_file",
    ]
    assert "run_command" not in fake_ai["tools"]
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

    s = _settings(tmp_path, MILL_TEST_COMMAND="pytest")
    monkeypatch.setattr(
        sandbox, "run", lambda cmd, *, repo_dir, settings: (0, "ok")
    )
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True and "passed" in fb


def test_test_agent_fail_distills_via_cheap_model(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = _settings(
        tmp_path, MILL_TEST_COMMAND="pytest", MILL_TEST_MODEL="test/cheap",
    )
    monkeypatch.setattr(
        sandbox, "run",
        lambda cmd, *, repo_dir, settings: (1, "E   assert 1 == 2\n" * 50),
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")

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


def test_test_agent_no_command_is_pass(tmp_path):
    s = _settings(tmp_path, MILL_TEST_COMMAND="")
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
