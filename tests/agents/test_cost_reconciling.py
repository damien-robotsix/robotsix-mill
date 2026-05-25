"""Tests for the cost-reconciliation agent."""

from robotsix_mill.agents.cost_reconciling import (
    CostReconciliationResult,
    run_cost_reconciliation_agent,
)


class _Result:
    def __init__(self, output):
        self.output = output


class _FakeAgent:
    def __init__(self, output=None):
        self.output = output or CostReconciliationResult(
            analysis="Test analysis",
            conclusion="Test conclusion",
        )
        self.calls = []

    def run_sync(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return _Result(self.output)


def _patch_agent(monkeypatch, agent):
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent", lambda *a, **k: agent
    )


def test_returns_cost_reconciliation_result(settings, monkeypatch):
    """Agent returns structured result on success."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    result = run_cost_reconciliation_agent(
        settings=settings,
        openrouter_total=5.00,
        langfuse_total=4.50,
        delta=0.50,
        openrouter_breakdown="gpt-4: $5.00",
        langfuse_breakdown="implement: $4.50",
    )
    assert isinstance(result, CostReconciliationResult)
    assert result.analysis == "Test analysis"
    assert result.conclusion == "Test conclusion"


def test_prompt_includes_all_data(settings, monkeypatch):
    """Prompt should include totals, delta, and both breakdowns."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    run_cost_reconciliation_agent(
        settings=settings,
        openrouter_total=12.34,
        langfuse_total=9.87,
        delta=2.47,
        openrouter_breakdown="gpt-4: $12.34",
        langfuse_breakdown="coordinator: $9.87",
    )
    assert len(agent.calls) == 1
    prompt, _ = agent.calls[0]
    assert "12.3400" in prompt
    assert "9.8700" in prompt
    assert "2.4700" in prompt
    assert "gpt-4: $12.34" in prompt
    assert "coordinator: $9.87" in prompt


def test_graceful_on_agent_error(settings, monkeypatch):
    """Agent failure returns fallback result (never raises)."""
    class _Boom:
        def run_sync(self, *a, **k):
            raise RuntimeError("model down")

    _patch_agent(monkeypatch, _Boom())
    result = run_cost_reconciliation_agent(
        settings=settings,
        openrouter_total=5.00,
        langfuse_total=5.00,
        delta=0.00,
        openrouter_breakdown="",
        langfuse_breakdown="",
    )
    assert isinstance(result, CostReconciliationResult)
    assert "failed" in result.conclusion.lower()


def test_non_result_output_handled(settings, monkeypatch):
    """Agent returning wrong type → fallback with warning."""
    class _Weird:
        def run_sync(self, *a, **k):
            return _Result("not a CostReconciliationResult")

    _patch_agent(monkeypatch, _Weird())
    result = run_cost_reconciliation_agent(
        settings=settings,
        openrouter_total=5.00,
        langfuse_total=5.00,
        delta=0.00,
        openrouter_breakdown="",
        langfuse_breakdown="",
    )
    assert isinstance(result, CostReconciliationResult)
    assert "unexpected type" in result.conclusion
