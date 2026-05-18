"""run_rebase_agent result handling (regression: it used the removed
pydantic-ai `.data` attr → AttributeError → every rebase BLOCKED)."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.agents.rebasing import run_rebase_agent
from robotsix_mill.config import Settings


def _s(tmp_path):
    return Settings(MILL_DATA_DIR=str(tmp_path), OPENROUTER_API_KEY="k")


@pytest.fixture
def fake_ai(monkeypatch):
    box = {}

    class FakeModel:
        def __init__(self, name, **kw): pass

    class FakeAgent:
        def __init__(self, **kw): pass

        def run_sync(self, *a, **k):
            # AgentRunResult has `.output` and NO `.data` (the old API).
            return type("R", (), {"output": box["out"]})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)
    return box


@pytest.mark.parametrize("out,expected", [
    ("DONE", True),
    ("done — rebased cleanly", True),
    ("FAILED: unresolvable conflict", False),
    ("", False),
])
def test_run_rebase_agent_reads_output_not_data(tmp_path, fake_ai, out, expected):
    fake_ai["out"] = out
    # must NOT raise AttributeError('.data'); must read .output
    assert run_rebase_agent(
        settings=_s(tmp_path), repo_dir=tmp_path,
        branch="mill/x", target="main",
    ) is expected
