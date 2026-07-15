"""run_ci_fix_agent result handling — mirrors test_rebasing.py."""

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents.ci_fixing import CiFixResult, run_ci_fix_agent
from robotsix_mill.agents.ci_patterns import CiPatternEntry
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _s(tmp_path):
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="k")
    return Settings(data_dir=str(tmp_path))


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
    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )
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


def test_ci_fix_result_out_of_scope_fields():
    """CiFixResult accepts status='OUT_OF_SCOPE' plus the three new fields,
    which default to empty strings so existing DONE/FAILED construction is
    unaffected."""
    # Existing construction unchanged: new fields default to "".
    done = CiFixResult(status="DONE", summary="ok")
    assert done.out_of_scope_reason == ""
    assert done.failing_check == ""
    assert done.required_change_area == ""

    oos = CiFixResult(
        status="OUT_OF_SCOPE",
        summary="not mine to fix",
        out_of_scope_reason="alert in __init__.py, outside this ticket's diff",
        failing_check="py/clear-text-logging",
        required_change_area="src/pkg/__init__.py",
    )
    assert oos.status == "OUT_OF_SCOPE"
    assert oos.out_of_scope_reason == "alert in __init__.py, outside this ticket's diff"
    assert oos.failing_check == "py/clear-text-logging"
    assert oos.required_change_area == "src/pkg/__init__.py"


def test_out_of_scope_skips_pattern_persistence(tmp_path, monkeypatch):
    """An OUT_OF_SCOPE verdict does not persist a fix-attempt pattern even
    when pattern_signature is set."""
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
                        status="OUT_OF_SCOPE",
                        summary="repo debt",
                        pattern_signature="py/some-rule",
                        failing_check="py/some-rule",
                        required_change_area="other.py",
                    )
                },
            )()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    from robotsix_mill.agents import fs_tools, ci_patterns

    monkeypatch.setattr(fs_tools, "build_fs_tools", lambda rd, s: [])

    calls = []
    monkeypatch.setattr(ci_patterns, "load_patterns", lambda path: [])
    monkeypatch.setattr(
        ci_patterns, "save_patterns", lambda path, entries: calls.append("called")
    )

    result = run_ci_fix_agent(
        settings=s,
        repo_dir=tmp_path,
        branch="mill/x",
        failing_summary="CodeQL alert",
        ticket_id="t-oos",
    )
    assert result.status == "OUT_OF_SCOPE"
    assert len(calls) == 0


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

    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

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

    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

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
    """Patterns from ci_patterns appear in the user prompt (not system prompt)."""
    s = _s(tmp_path)
    captured_prompt = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured_prompt["system_prompt"] = kw.get("system_prompt", "")

        def run_sync(self, prompt, *a, **k):
            captured_prompt["user_prompt"] = prompt
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

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
    # Patterns block lives in the user prompt now, not system prompt.
    user = captured_prompt["user_prompt"]
    assert "[SUCCESS, 1 attempt(s)]" in user
    assert "E501 line too long" in user
    assert "used edit_file to wrap line" in user
    # Patterns section must NOT be in system prompt.
    assert "(no prior patterns" not in captured_prompt["system_prompt"]


def test_no_patterns_shows_placeholder(tmp_path, monkeypatch):
    """When no patterns match, no patterns section is injected."""
    s = _s(tmp_path)
    captured_prompt = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured_prompt["system_prompt"] = kw.get("system_prompt", "")

        def run_sync(self, prompt, *a, **k):
            captured_prompt["user_prompt"] = prompt
            return type("R", (), {"output": CiFixResult(status="DONE", summary="ok")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

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
    # When no patterns match, the patterns section is NOT injected at all.
    user = captured_prompt["user_prompt"]
    assert "(no prior patterns" not in user
    assert "Prior fix attempts" not in user
    assert "(no prior patterns" not in captured_prompt["system_prompt"]


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

    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

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

    monkeypatch.setattr(
        "robotsix_mill.agents.base.new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

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
