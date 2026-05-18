"""Coordinator / implement-worker / test sub-agent — the reworked
delegation architecture (per-agent models, no deep layer)."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents import coordinating, implement_worker, testing
from robotsix_mill.agents import openrouter_cost as oc
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

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["limit"] = getattr(usage_limits, "request_limit", None)
            return type("R", (), {"output": "  did it  "})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)
    return cap


def test_implement_worker_uses_implement_model_and_fs_only(
    tmp_path, fake_ai
):
    s = _settings(
        tmp_path, MILL_MODEL="coord/big", MILL_IMPLEMENT_MODEL="impl/cap",
    )
    out = implement_worker.run_implement_worker(
        settings=s, repo_dir=tmp_path, instructions="do X precisely"
    )
    assert out == "did it"  # stripped
    assert fake_ai["model"] == "impl/cap"  # its own model
    # file tools only — never run_command (no shell/tests/git)
    assert fake_ai["tools"] == ["list_dir", "read_file", "write_file"]


def test_coordinator_uses_coordinator_model_and_delegation_tools(
    tmp_path, fake_ai
):
    s = _settings(
        tmp_path, MILL_MODEL="coord/big",
        MILL_COORDINATOR_REQUEST_LIMIT="9",
    )
    out = coordinating.run_coordinator(
        settings=s, repo_dir=tmp_path, spec="build a thing"
    )
    assert out == "did it"
    assert fake_ai["model"] == "coord/big"
    assert fake_ai["limit"] == 9
    # coordinator only orchestrates: explore + delegated impl + tests
    # + web_research (added by web=True). No raw fs/shell tools.
    assert fake_ai["tools"] == [
        "explore", "implement", "run_tests", "web_research"
    ]


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
            pass

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


def test_test_agent_no_command_is_pass(tmp_path):
    s = _settings(tmp_path, MILL_TEST_COMMAND="")
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True
