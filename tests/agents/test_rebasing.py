"""run_rebase_agent result handling (regression: it used the removed
pydantic-ai `.data` attr → AttributeError → every rebase BLOCKED),
plus API-key guard, agent construction, system prompt, and output
edge cases."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.agents.rebasing import RebaseResult, run_rebase_agent
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _s(tmp_path, **kw):
    kw.setdefault("OPENROUTER_API_KEY", "k")
    kw.setdefault("data_dir", str(tmp_path))
    # Mirror openrouter_api_key into Secrets so get_secrets() works
    key = kw.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    return Settings(**kw)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ai(monkeypatch):
    """Monkeypatch the pydantic-ai layer so no real LLM is called."""
    box = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, *a, **k):
            return type(
                "R",
                (),
                {
                    "output": RebaseResult(
                        status=box["status"],
                        summary=box.get("summary", ""),
                    )
                },
            )()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)
    return box


# ---------------------------------------------------------------------------
# Existing parametrized test (output parsing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("DONE", True),
        ("FAILED", False),
    ],
)
def test_run_rebase_agent_reads_output_not_data(tmp_path, fake_ai, status, expected):
    fake_ai["status"] = status
    result = run_rebase_agent(
        settings=_s(tmp_path),
        repo_dir=tmp_path,
        branch="mill/x",
        target="main",
    )
    assert (result.status == "DONE") is expected


# ---------------------------------------------------------------------------
# API-key guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, ""])
def test_run_rebase_agent_raises_when_api_key_falsy(tmp_path, key):
    """Must raise BEFORE any agent is built."""
    s = _s(tmp_path, OPENROUTER_API_KEY=key)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        run_rebase_agent(
            settings=s,
            repo_dir=tmp_path,
            branch="mill/x",
            target="main",
        )


# ---------------------------------------------------------------------------
# Agent construction: verify args passed to build_agent
# ---------------------------------------------------------------------------


def test_build_agent_called_with_web_false_and_settings(tmp_path, monkeypatch):
    """build_agent is called with web_knowledge=False, a Settings, and shell tools."""
    captured = {}

    def fake_build_agent(
        settings, *, system_prompt, output_type, tools, web_knowledge, **kw
    ):
        captured["settings"] = settings
        captured["system_prompt"] = system_prompt
        captured["output_type"] = output_type
        captured["tools"] = tools
        captured["web_knowledge"] = web_knowledge

        # Return a fake agent whose run_sync returns DONE.
        class FakeAgent:
            def run_sync(self, *a, **k):
                return type(
                    "R", (), {"output": RebaseResult(status="DONE", summary="ok")}
                )()

        return FakeAgent()

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)

    s = _s(tmp_path)
    result = run_rebase_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        target="main",
    )
    assert result.status == "DONE"
    assert captured["web_knowledge"] is False
    assert isinstance(captured["settings"], Settings)
    # output_type is now PromptedOutput(RebaseResult), not str
    from pydantic_ai import PromptedOutput

    assert isinstance(captured["output_type"], PromptedOutput)


def test_tools_include_shell_tools(tmp_path, monkeypatch):
    """The tools list passed to build_agent includes run_command and friends."""
    captured_tools = []

    def fake_build_agent(
        settings, *, system_prompt, output_type, tools, web_knowledge, **kw
    ):
        captured_tools.extend(tools or [])

        class FakeAgent:
            def run_sync(self, *a, **k):
                return type(
                    "R", (), {"output": RebaseResult(status="DONE", summary="ok")}
                )()

        return FakeAgent()

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)

    s = _s(tmp_path)
    run_rebase_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        target="main",
    )

    tool_names = {getattr(t, "__name__", str(t)) for t in captured_tools}
    assert "run_command" in tool_names, f"shell tool missing; got {tool_names}"
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "edit_file" in tool_names
    assert "list_dir" in tool_names
    assert "git_fetch" in tool_names
    assert "git_push_with_lease" in tool_names


# ---------------------------------------------------------------------------
# System prompt content
# ---------------------------------------------------------------------------


def test_system_prompt_contains_key_instructions(tmp_path, monkeypatch):
    """The system_prompt captures the rebase workflow."""
    captured_prompt = []

    def fake_build_agent(
        settings, *, system_prompt, output_type, tools, web_knowledge, **kw
    ):
        captured_prompt.append(system_prompt)

        class FakeAgent:
            def run_sync(self, *a, **k):
                return type(
                    "R", (), {"output": RebaseResult(status="DONE", summary="ok")}
                )()

        return FakeAgent()

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)

    s = _s(tmp_path)
    run_rebase_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        target="main",
    )

    prompt = captured_prompt[0]
    assert "git rebase origin/" in prompt
    assert "git rebase --continue" in prompt
    assert "DONE" in prompt
    assert "FAILED" in prompt


def test_rebase_falls_back_to_deepseek_when_primary_fails(tmp_path, monkeypatch):
    """Regression: rebase must invoke via run_agent so a Claude outage
    (primary raises) falls back to the DeepSeek handle instead of hard-failing
    and blocking the ticket. Before the fix rebase called agent.run_sync()
    bare, so FallbackAgentHandle never fell back and a credit-exhausted Claude
    blocked every rebase with 'rebase failed after 3 attempts'."""
    from robotsix_mill.agents import base as agents_base
    from robotsix_mill.agents.fallback import FallbackAgentHandle

    class _Primary:
        def run_sync(self, *a, **k):
            raise RuntimeError("Claude Code returned an error result: success")

        def close(self):
            pass

    class _Fallback:
        def run_sync(self, *a, **k):
            return type(
                "R",
                (),
                {"output": RebaseResult(status="DONE", summary="rebased via deepseek")},
            )()

        def close(self):
            pass

    built = {"n": 0}

    def _build_fallback():
        built["n"] += 1
        return _Fallback()

    handle = FallbackAgentHandle(_Primary(), _build_fallback)
    monkeypatch.setattr(
        agents_base, "build_agent_from_definition", lambda *a, **k: handle
    )

    result = run_rebase_agent(
        settings=_s(tmp_path),
        repo_dir=tmp_path,
        branch="mill/x",
        target="main",
    )
    assert result.status == "DONE"
    assert result.summary == "rebased via deepseek"
    assert built["n"] == 1  # the DeepSeek fallback was actually built + used
