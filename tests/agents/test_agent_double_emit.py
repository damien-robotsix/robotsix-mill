"""Regression: agents that emit drafts via structured output must NOT
also have the report_issue tool.

Audit, retrospect, agent_check, and health all return ``PromptedOutput``
results that the runner turns into draft tickets. If they ALSO carry
the ``report_issue`` tool the agent files drafts through both channels
at once — the structured emit path and the tool — leading to duplicate
tickets and confused dedup. This test pins each of those four agent
entry points to ``report_issue=False``.

This contract regressed twice in 24 hours: PR #98 added ``report_issue=False``
to health.py, then PR #94 (a docstring add on an older base branch)
auto-merged on top and silently dropped the kwarg. With this test the
in-sandbox suite catches that class of regression *before* the gate
goes green.
"""

from __future__ import annotations


def _collect_build_agent_kwargs(monkeypatch, agent_module, callable_name, **call_kwargs):
    """Monkeypatch ``build_agent`` to capture its kwargs without actually
    constructing an LLM client, then invoke the agent's entry function.

    Returns the captured kwargs dict (raises if build_agent wasn't
    called exactly once)."""
    captured: list[dict] = []

    class _FakeAgent:
        def run_sync(self, *_args, **_kwargs):
            class _R:
                output = ""
            return _R()

    def _fake_build_agent(*_args, **kwargs):
        captured.append(kwargs)
        return _FakeAgent()

    # Each agent module imports build_agent lazily inside its function
    # (`from .base import build_agent`), so monkeypatching the symbol on
    # the agent module is too late — the import resolves to base.py at
    # call time. Patch the source instead.
    import robotsix_mill.agents.base as base_mod
    monkeypatch.setattr(base_mod, "build_agent", _fake_build_agent)
    # Also stub call_with_retry so the fake .run_sync result isn't
    # double-wrapped or retried.
    import robotsix_mill.agents.retry as retry_mod
    monkeypatch.setattr(
        retry_mod, "call_with_retry",
        lambda fn, **_: fn(),
    )

    from robotsix_mill.config import Settings
    s = Settings()
    fn = getattr(__import__(
        f"robotsix_mill.agents.{agent_module}", fromlist=[callable_name],
    ), callable_name)
    try:
        fn(settings=s, **call_kwargs)
    except Exception:
        # The fake .run_sync output is empty; the agent's downstream
        # parsing may raise. That's fine — build_agent was called
        # before parsing, so the kwarg is already captured.
        pass

    assert len(captured) == 1, f"build_agent calls: {len(captured)}"
    return captured[0]


def test_audit_agent_has_report_issue_false(monkeypatch):
    kw = _collect_build_agent_kwargs(monkeypatch, "auditing", "run_audit_agent")
    assert kw.get("report_issue") is False, (
        f"audit emits via PromptedOutput; report_issue must be False "
        f"(got {kw.get('report_issue')!r})"
    )


def test_retrospect_agent_has_report_issue_false(monkeypatch):
    kw = _collect_build_agent_kwargs(
        monkeypatch, "retrospecting", "run_retrospect_agent",
        ticket_summary="", history_text="", langfuse_summary=None,
        memory="",
    )
    assert kw.get("report_issue") is False, (
        f"retrospect emits via PromptedOutput; report_issue must be False "
        f"(got {kw.get('report_issue')!r})"
    )


def test_agent_check_has_report_issue_false(monkeypatch):
    kw = _collect_build_agent_kwargs(
        monkeypatch, "agent_check", "run_agent_check_agent",
        memory="",
    )
    assert kw.get("report_issue") is False, (
        f"agent_check emits via PromptedOutput; report_issue must be False "
        f"(got {kw.get('report_issue')!r})"
    )


def test_health_agent_has_report_issue_false(monkeypatch):
    kw = _collect_build_agent_kwargs(
        monkeypatch, "health", "run_health_agent",
        memory="",
    )
    assert kw.get("report_issue") is False, (
        f"health emits via PromptedOutput; report_issue must be False "
        f"(got {kw.get('report_issue')!r})"
    )
