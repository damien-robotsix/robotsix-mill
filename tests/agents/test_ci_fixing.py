"""run_ci_fix_agent result handling — mirrors test_rebasing.py."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents import openrouter_cost as oc
from robotsix_mill.agents.ci_fixing import CiFixResult, run_ci_fix_agent
from robotsix_mill.agents.ci_patterns import CiPatternEntry
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _s(tmp_path):
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="k")
    return Settings(data_dir=str(tmp_path), OPENROUTER_API_KEY="k")


@pytest.fixture
def fake_ai(monkeypatch):
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
                    "output": CiFixResult(
                        status=box["status"],
                        summary=box.get("summary", ""),
                    )
                },
            )()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)
    return box


@pytest.mark.parametrize(
    "status,expected",
    [
        ("DONE", True),
        ("FAILED", False),
    ],
)
def test_run_ci_fix_agent_reads_output(tmp_path, fake_ai, status, expected):
    """B.9/B.10: Agent returns True on DONE, False otherwise."""
    fake_ai["status"] = status
    result = run_ci_fix_agent(
        settings=_s(tmp_path),
        repo_dir=tmp_path,
        branch="mill/x",
        failing_summary="lint failed",
        ticket_id="test-123",
    )
    assert (result.status == "DONE") is expected


def test_missing_api_key_raises(tmp_path):
    """B.11: Raises RuntimeError when OPENROUTER_API_KEY is missing."""
    s = Settings(data_dir=str(tmp_path))  # no key
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        run_ci_fix_agent(
            settings=s,
            repo_dir=tmp_path,
            branch="mill/x",
            failing_summary="x",
            ticket_id="",
        )


def test_uses_build_fs_tools(tmp_path, monkeypatch):
    """B.12: Uses build_fs_tools with correct repo_dir."""
    s = _s(tmp_path)
    seen_calls = {}

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, *a, **k):
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    # build_fs_tools is imported from .fs_tools in the function body,
    # so monkeypatch at the source module.
    from robotsix_mill.agents import fs_tools

    def fake_build_fs_tools(repo_dir, settings):
        seen_calls["repo_dir"] = str(repo_dir)
        return []

    monkeypatch.setattr(fs_tools, "build_fs_tools", fake_build_fs_tools)

    run_ci_fix_agent(
        settings=s,
        repo_dir=tmp_path / "the_repo",
        branch="mill/x",
        failing_summary="x",
        ticket_id="",
    )
    assert "the_repo" in seen_calls["repo_dir"]


def test_agent_prompt_forbids_push_and_branch_switching(tmp_path, monkeypatch):
    """B.13: The system prompt forbids git push and branch switching."""
    s = _s(tmp_path)
    captured_prompt = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured_prompt["system_prompt"] = kw.get("system_prompt", "")

        def run_sync(self, *a, **k):
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents import fs_tools

    monkeypatch.setattr(fs_tools, "build_fs_tools", lambda rd, s: [])

    run_ci_fix_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        failing_summary="x",
        ticket_id="",
    )
    prompt = captured_prompt["system_prompt"]
    assert "NEVER push" in prompt


# ---------------------------------------------------------------------------
# New pattern-memory tests
# ---------------------------------------------------------------------------


def test_patterns_injected_into_prompt(tmp_path, monkeypatch):
    """Patterns from ci_patterns appear in the system prompt."""
    s = _s(tmp_path)
    captured_prompt = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured_prompt["system_prompt"] = kw.get("system_prompt", "")

        def run_sync(self, *a, **k):
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents import fs_tools, ci_patterns

    monkeypatch.setattr(fs_tools, "build_fs_tools", lambda rd, s: [])
    monkeypatch.setattr(
        ci_patterns,
        "load_patterns",
        lambda path: [
            CiPatternEntry(
                category="lint_error",
                signature="E501 line too long",
                approach="used edit_file to wrap line",
                success=True,
                attempts=1,
                ticket_id="abc",
                timestamp="2025-01-01T00:00:00+00:00",
            ),
        ],
    )

    run_ci_fix_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        failing_summary="E501 line too long",
        ticket_id="test-1",
    )
    prompt = captured_prompt["system_prompt"]
    assert "[SUCCESS, 1 attempt(s)]" in prompt
    assert "E501 line too long" in prompt
    assert "used edit_file to wrap line" in prompt


def test_no_patterns_shows_placeholder(tmp_path, monkeypatch):
    """When no patterns match, the '(no prior patterns)' placeholder appears."""
    s = _s(tmp_path)
    captured_prompt = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured_prompt["system_prompt"] = kw.get("system_prompt", "")

        def run_sync(self, *a, **k):
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents import fs_tools, ci_patterns

    monkeypatch.setattr(fs_tools, "build_fs_tools", lambda rd, s: [])
    monkeypatch.setattr(ci_patterns, "load_patterns", lambda path: [])

    run_ci_fix_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        failing_summary="some failure",
        ticket_id="test-2",
    )
    prompt = captured_prompt["system_prompt"]
    assert "(no prior patterns for this failure)" in prompt


def test_pattern_saved_after_fix(tmp_path, monkeypatch):
    """When the agent returns a pattern_signature, it is saved."""
    s = _s(tmp_path)

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, *a, **k):
            return type(
                "R",
                (),
                {
                    "output": CiFixResult(
                        status="DONE",
                        summary="fixed lint",
                        pattern_category="lint_error",
                        pattern_signature="E501 line too long",
                        pattern_approach="wrapped the line",
                    )
                },
            )()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents import fs_tools, ci_patterns

    monkeypatch.setattr(fs_tools, "build_fs_tools", lambda rd, s: [])

    saved_entries: list = []

    def fake_save(path, entries):
        saved_entries.extend(entries)

    monkeypatch.setattr(ci_patterns, "load_patterns", lambda path: [])
    monkeypatch.setattr(ci_patterns, "save_patterns", fake_save)

    run_ci_fix_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        failing_summary="E501",
        ticket_id="my-ticket",
    )
    assert len(saved_entries) == 1
    e = saved_entries[0]
    assert e.category == "lint_error"
    assert e.signature == "E501 line too long"
    assert e.approach == "wrapped the line"
    assert e.success is True
    assert e.attempts == 1
    assert e.ticket_id == "my-ticket"


def test_no_pattern_saved_when_signature_empty(tmp_path, monkeypatch):
    """When pattern_signature is empty, save_patterns is NOT called."""
    s = _s(tmp_path)

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, *a, **k):
            return type(
                "R",
                (),
                {
                    "output": CiFixResult(
                        status="FAILED",
                        summary="could not fix",
                        pattern_signature="",
                    )
                },
            )()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    from robotsix_mill.agents import fs_tools, ci_patterns

    monkeypatch.setattr(fs_tools, "build_fs_tools", lambda rd, s: [])

    calls = []

    def fake_save(path, entries):
        calls.append("called")

    monkeypatch.setattr(ci_patterns, "load_patterns", lambda path: [])
    monkeypatch.setattr(ci_patterns, "save_patterns", fake_save)

    run_ci_fix_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        failing_summary="E501",
        ticket_id="my-ticket",
    )
    assert len(calls) == 0
