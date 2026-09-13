"""Tests for the mypy_baseline agent and its dynamic-kwargs builder."""

from robotsix_mill.agents import mypy_baseline
from robotsix_mill.config import Settings


def test_mypy_baseline_system_prompt_is_non_empty_string():
    """SYSTEM_PROMPT re-exports the YAML system_prompt without env-var resolution."""
    assert isinstance(mypy_baseline.SYSTEM_PROMPT, str)
    assert len(mypy_baseline.SYSTEM_PROMPT) > 0


def test_mypy_baseline_max_gaps():
    """MAX_GAPS is the expected constant."""
    assert mypy_baseline.MAX_GAPS == 5


def test_run_mypy_baseline_agent_wires_runner(monkeypatch):
    """run_mypy_baseline_agent delegates to run_periodic_agent with the
    expected flags, definition_name, and an explicit usage_limits."""
    captured: dict = {}

    def fake_run_periodic(**kwargs):
        captured["kwargs"] = kwargs
        return mypy_baseline.MyPyBaselineResult()

    from robotsix_mill.agents import periodic_base

    monkeypatch.setattr(periodic_base, "run_periodic_agent", fake_run_periodic)

    mypy_baseline.run_mypy_baseline_agent(settings=Settings())

    kw = captured["kwargs"]
    assert kw["definition_name"] == "mypy_baseline"
    assert kw["max_gaps"] == 5
    assert kw["include_run_command"] is True
    assert kw["include_parallel_commands"] is True
    # Regression: the runner previously inherited pydantic-ai's implicit
    # request_limit of 50 — it must now pass an explicit budget.
    assert kw["usage_limits"] is not None
    assert kw["usage_limits"].request_limit == 80


def test_mypy_baseline_dynamic_kwargs_default_request_limit():
    """Default Settings produce an explicit request_limit of 80."""
    from pydantic_ai.usage import UsageLimits

    kwargs = mypy_baseline._mypy_baseline_dynamic_kwargs(Settings())
    limits = kwargs["usage_limits"]
    assert isinstance(limits, UsageLimits)
    assert limits.request_limit == 80


def test_mypy_baseline_dynamic_kwargs_non_default_request_limit():
    """A non-default mypy_baseline_request_limit propagates into usage_limits."""
    kwargs = mypy_baseline._mypy_baseline_dynamic_kwargs(
        Settings(mypy_baseline_request_limit=123)
    )
    assert kwargs["usage_limits"].request_limit == 123
