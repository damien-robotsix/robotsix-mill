"""Unit tests for ``robotsix_mill.agents.coding`` — the implement seam
that wraps ``run_coordinator`` and translates its exceptions into
stage-resumable :class:`AgentBudgetError` / :class:`AgentRunError`.

Does NOT test the coordinator logic itself (that belongs in
``tests/agents/test_coordinating.py``) or stage-level handling of
these exceptions (covered by ``tests/stages/test_implement.py``).
"""

from __future__ import annotations

import pytest

from robotsix_mill.agents.coding import (
    AgentBudgetError,
    AgentRunError,
    run_implement_agent,
)
from robotsix_mill.agents.coordinating import ImplementResult


# ------------------------------------------------------------------
# Helper: install fake pydantic-ai exception classes into the coding
# module's namespace AND into ``pydantic_ai.exceptions`` so the local
# ``from pydantic_ai.exceptions import UsageLimitExceeded,
# UnexpectedModelBehavior`` inside ``run_implement_agent`` picks them up.
# ------------------------------------------------------------------


class _FakeUsageLimitExceeded(Exception):
    pass


_FakeUsageLimitExceeded.__name__ = "UsageLimitExceeded"


class _FakeUnexpectedModelBehavior(Exception):
    pass


_FakeUnexpectedModelBehavior.__name__ = "UnexpectedModelBehavior"


def _install_fake_exceptions(monkeypatch):
    """Make ``run_implement_agent``'s local import resolve to our
    fake classes instead of the real pydantic-ai ones."""
    import pydantic_ai.exceptions as _pexc

    monkeypatch.setattr(_pexc, "UsageLimitExceeded", _FakeUsageLimitExceeded)
    monkeypatch.setattr(
        _pexc,
        "UnexpectedModelBehavior",
        _FakeUnexpectedModelBehavior,
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_result(
    summary="done",
    updated_memory="memory content",
    reference_files=None,
    conversation_state=b"conv",
):
    return ImplementResult(
        summary=summary,
        updated_memory=updated_memory,
        reference_files=reference_files or [],
        conversation_state=conversation_state,
    )


def _call(settings, tmp_path, **kwargs):
    defaults = dict(
        settings=settings,
        repo_dir=tmp_path,
        spec="do X",
        memory="ledger",
        feedback="test failed",
        reference_files=[{"path": "a.py"}],
        message_history=["msg"],
        epic_context="epic",
        previous_attempt_summary="prev",
        board_id="b",
    )
    defaults.update(kwargs)
    return run_implement_agent(**defaults)


# ------------------------------------------------------------------
# 8. Exception class behaviour (no mocks needed)
# ------------------------------------------------------------------


def test_agent_budget_error_is_runtimeerror():
    assert issubclass(AgentBudgetError, RuntimeError)


def test_agent_run_error_is_runtimeerror():
    assert issubclass(AgentRunError, RuntimeError)


def test_agent_budget_error_messages_attribute():
    e = AgentBudgetError("msg", [1, 2, 3])
    assert e.messages == [1, 2, 3]
    assert str(e) == "msg"


def test_agent_run_error_messages_attribute():
    e = AgentRunError("msg", ["a", "b"])
    assert e.messages == ["a", "b"]
    assert str(e) == "msg"


# ------------------------------------------------------------------
# 1. Happy path — parameter passthrough and return tuple
# ------------------------------------------------------------------


def test_happy_path_passthrough(settings, tmp_path, monkeypatch):
    """All kwargs pass through to run_coordinator; return tuple
    matches ImplementResult fields."""
    calls: list[dict] = []
    result = _make_result(
        summary="s",
        updated_memory="um",
        reference_files=["f.py"],
        conversation_state=b"cs",
    )

    def _fake_run_coordinator(**kw):
        calls.append(kw)
        return result

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    out = _call(settings, tmp_path)

    # Exactly one call
    assert len(calls) == 1
    kw = calls[0]

    # Every kwarg passed through unchanged
    assert kw["settings"] is settings
    assert kw["repo_dir"] == tmp_path
    assert kw["spec"] == "do X"
    assert kw["memory"] == "ledger"
    assert kw["feedback"] == "test failed"
    assert kw["reference_files"] == [{"path": "a.py"}]
    assert kw["message_history"] == ["msg"]
    assert kw["epic_context"] == "epic"
    assert kw["previous_attempt_summary"] == "prev"
    assert kw["board_id"] == "b"

    # Result tuple — first 4 fields match ImplementResult.
    # Note: origin/main still carries a 5th element (new_messages);
    # we assert on the common prefix only.
    assert out[:4] == ("s", ["f.py"], "um", b"cs")


# ------------------------------------------------------------------
# 2. UsageLimitExceeded → AgentBudgetError
# ------------------------------------------------------------------


def test_usage_limit_exceeded_raises_agent_budget_error(
    settings,
    tmp_path,
    monkeypatch,
):
    _install_fake_exceptions(monkeypatch)

    def _fake_run_coordinator(**kw):
        raise _FakeUsageLimitExceeded("budget cap hit")

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    with pytest.raises(AgentBudgetError) as exc_info:
        _call(settings, tmp_path)

    e = exc_info.value
    assert "budget cap hit" in str(e)
    assert e.messages == []
    assert isinstance(e.__cause__, _FakeUsageLimitExceeded)
    assert e.__cause__.args[0] == "budget cap hit"


# ------------------------------------------------------------------
# 3. UnexpectedModelBehavior → fallback → success
# ------------------------------------------------------------------


def test_unexpected_model_behavior_fallback_success(
    settings,
    tmp_path,
    monkeypatch,
):
    _install_fake_exceptions(monkeypatch)

    primary_calls: list[dict] = []
    fallback_calls: list[dict] = []
    result = _make_result(summary="fallback ok")

    def _fake_run_coordinator(**kw):
        if not primary_calls:
            primary_calls.append(kw)
            raise _FakeUnexpectedModelBehavior("output retries exhausted")
        fallback_calls.append(kw)
        return result

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    out = _call(settings, tmp_path)

    # Primary call: no model_name
    assert len(primary_calls) == 1
    assert primary_calls[0].get("model_name") is None

    # Fallback call: model_name="deepseek/deepseek-v4-flash"
    assert len(fallback_calls) == 1
    assert fallback_calls[0].get("model_name") == "deepseek/deepseek-v4-flash"

    # Returns normally
    assert out[0] == "fallback ok"


# ------------------------------------------------------------------
# 4. UnexpectedModelBehavior → fallback also fails → AgentRunError
# ------------------------------------------------------------------


def test_unexpected_model_behavior_fallback_also_fails(
    settings,
    tmp_path,
    monkeypatch,
):
    _install_fake_exceptions(monkeypatch)

    call_count = {"n": 0}

    def _fake_run_coordinator(**kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _FakeUnexpectedModelBehavior("primary exhausted")
        raise ValueError("fallback crashed")

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    with pytest.raises(AgentRunError) as exc_info:
        _call(settings, tmp_path)

    e = exc_info.value
    assert "output retries exhausted on primary + fallback models" in str(e)
    assert "primary=" in str(e)
    assert "fallback=" in str(e)
    assert e.messages == []
    # __cause__ is the primary UnexpectedModelBehavior
    assert isinstance(e.__cause__, _FakeUnexpectedModelBehavior)


# ------------------------------------------------------------------
# 5. AgentBudgetError / AgentRunError re-raised unchanged
# ------------------------------------------------------------------


def test_agent_budget_error_re_raised_unchanged(
    settings,
    tmp_path,
    monkeypatch,
):
    original = AgentBudgetError("budget", [{"info": 1}])

    def _fake_run_coordinator(**kw):
        raise original

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    with pytest.raises(AgentBudgetError) as exc_info:
        _call(settings, tmp_path)

    assert exc_info.value is original
    assert exc_info.value.messages == [{"info": 1}]
    assert str(exc_info.value) == "budget"


def test_agent_run_error_re_raised_unchanged(
    settings,
    tmp_path,
    monkeypatch,
):
    original = AgentRunError("run err", ["detail"])

    def _fake_run_coordinator(**kw):
        raise original

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    with pytest.raises(AgentRunError) as exc_info:
        _call(settings, tmp_path)

    assert exc_info.value is original
    assert exc_info.value.messages == ["detail"]
    assert str(exc_info.value) == "run err"


# ------------------------------------------------------------------
# 6. Generic Exception → AgentRunError
# ------------------------------------------------------------------


def test_generic_exception_wraps_to_agent_run_error(
    settings,
    tmp_path,
    monkeypatch,
):
    def _fake_run_coordinator(**kw):
        raise ValueError("something broke")

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    with pytest.raises(AgentRunError) as exc_info:
        _call(settings, tmp_path)

    e = exc_info.value
    assert str(e) == "something broke"
    assert e.messages == []
    assert isinstance(e.__cause__, ValueError)
    assert e.__cause__.args[0] == "something broke"
    # cause is the typed original — used downstream by the implement
    # stage to classify transient infra failures and route to retry.
    assert e.cause is e.__cause__


# ------------------------------------------------------------------
# 7. Explore budget exhausted after success
# ------------------------------------------------------------------


def test_explore_budget_exhausted_raises_agent_budget_error(
    settings,
    tmp_path,
    monkeypatch,
):
    result = _make_result(summary="done")
    reset_calls: list = []

    def _fake_run_coordinator(**kw):
        return result

    def _fake_is_exhausted():
        return True

    def _fake_reset():
        reset_calls.append(1)

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.explore.is_explore_budget_exhausted",
        _fake_is_exhausted,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.explore.reset_explore_budget_exhausted",
        _fake_reset,
    )

    with pytest.raises(AgentBudgetError) as exc_info:
        _call(settings, tmp_path)

    e = exc_info.value
    assert "explore sub-agent exceeded request_limit=" in str(e)
    assert str(settings.explore_request_limit) in str(e)
    assert "coordinator could not proceed without exploration" in str(e)
    assert e.messages == []

    # reset is called
    assert len(reset_calls) == 1


def test_explore_budget_exhausted_reset_not_called_on_error(
    settings,
    tmp_path,
    monkeypatch,
):
    """When the coordinator raises UsageLimitExceeded, the explore
    budget check is never reached — reset must NOT be called."""
    _install_fake_exceptions(monkeypatch)

    reset_calls: list = []

    def _fake_run_coordinator(**kw):
        raise _FakeUsageLimitExceeded("cap")

    def _fake_reset():
        reset_calls.append(1)

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.explore.reset_explore_budget_exhausted",
        _fake_reset,
    )

    with pytest.raises(AgentBudgetError):
        _call(settings, tmp_path)

    assert len(reset_calls) == 0


# ------------------------------------------------------------------
# Edge: default parameters
# ------------------------------------------------------------------


def test_default_parameters_passthrough(settings, tmp_path, monkeypatch):
    """When optional parameters are omitted they pass through as their
    defaults (e.g. feedback=None, memory="")."""
    calls: list[dict] = []
    result = _make_result()

    def _fake_run_coordinator(**kw):
        calls.append(kw)
        return result

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    run_implement_agent(settings=settings, repo_dir=tmp_path, spec="do X")

    kw = calls[0]
    assert kw["feedback"] is None
    assert kw["memory"] == ""
    assert kw["reference_files"] is None
    assert kw["message_history"] is None
    assert kw["epic_context"] == ""
    assert kw["previous_attempt_summary"] is None
    assert kw["board_id"] == ""
    assert kw["language_instructions"] == ""


def test_extra_roots_forwards_to_run_coordinator(
    settings, tmp_path, monkeypatch
):
    """``extra_roots`` is forwarded to the inner ``run_coordinator``
    calls (both the primary path and the deepseek-fallback path)."""
    calls: list[dict] = []
    result = _make_result()

    def _fake_run_coordinator(**kw):
        calls.append(kw)
        return result

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    roots = [tmp_path / "clone_a", tmp_path / "clone_b"]
    run_implement_agent(
        settings=settings,
        repo_dir=tmp_path,
        spec="do X",
        extra_roots=roots,
    )

    # Primary path captured extra_roots.
    assert len(calls) == 1
    assert calls[0]["extra_roots"] == roots


def test_extra_roots_forwards_on_fallback(
    settings, tmp_path, monkeypatch
):
    """When the primary model raises ``UnexpectedModelBehavior``,
    the fallback ``run_coordinator`` call also receives ``extra_roots``."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    calls: list[dict] = []
    result = _make_result()

    call_count = 0

    def _fake_run_coordinator(**kw):
        nonlocal call_count
        calls.append(kw)
        call_count += 1
        if call_count == 1:
            raise UnexpectedModelBehavior("output retries exhausted")
        return result

    monkeypatch.setattr(
        "robotsix_mill.agents.coordinating.run_coordinator",
        _fake_run_coordinator,
    )

    roots = [tmp_path / "clone_c"]
    run_implement_agent(
        settings=settings,
        repo_dir=tmp_path,
        spec="do X",
        extra_roots=roots,
    )

    assert len(calls) == 2
    # Both primary and fallback calls receive extra_roots.
    assert calls[0]["extra_roots"] == roots
    assert calls[1]["extra_roots"] == roots
