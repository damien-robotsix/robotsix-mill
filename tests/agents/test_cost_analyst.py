"""Tests for the cost-analyst agent — prompt dimensions, result schema,
and parallel-list clipping."""

from types import SimpleNamespace

import pytest

from robotsix_mill.agents import cost_analyst as ca
from robotsix_mill.config import Settings


def test_cost_analyst_prompt_covers_lever_taxonomy():
    p = ca.SYSTEM_PROMPT.lower()
    # The four cost levers.
    for cue in ("over-provision", "token bloat", "cycle", "redundant tool"):
        assert cue in p, f"prompt missing lever cue: {cue}"
    # The four significant specimens.
    for cue in (
        "most expensive trace",
        "most expensive ticket",
        "most errors",
        "most steps",
    ):
        assert cue in p, f"prompt missing specimen cue: {cue}"
    # High-confidence discipline + estimate-not-measure framing.
    assert "high-confidence" in p
    assert "estimate" in p
    # Aggregates across all repos, not one.
    assert "all reg" in p or "across all" in p


def test_cost_reduction_result_schema():
    r = ca.CostReductionResult(
        draft_titles=["a"], draft_bodies=["b"], gap_ids=["g"], updated_memory="m"
    )
    assert r.draft_titles == ["a"] and r.gap_ids == ["g"]
    # Defaults are empty.
    empty = ca.CostReductionResult()
    assert empty.draft_titles == [] and empty.updated_memory == ""


def test_run_clips_parallel_lists(monkeypatch):
    """run_cost_analyst_agent clips the parallel draft lists to MAX_PROPOSALS
    in lockstep and tolerates ragged lists."""
    n = ca.MAX_PROPOSALS + 5
    fat = ca.CostReductionResult(
        draft_titles=[f"t{i}" for i in range(n)],
        draft_bodies=[f"b{i}" for i in range(n)],
        gap_ids=[f"g{i}" for i in range(n)],
        updated_memory="mem",
    )

    monkeypatch.setattr(ca, "_run_unused", None, raising=False)
    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry, "run_agent", lambda agent, fn, **k: SimpleNamespace(output=fat)
    )

    out = ca.run_cost_analyst_agent(
        settings=Settings(data_dir="/tmp/ca-test"),
        memory="",
        recent_proposals="",
        digest="<aggregate-cost-by-stage>x</aggregate-cost-by-stage>",
    )
    assert len(out.draft_titles) == ca.MAX_PROPOSALS
    assert len(out.draft_bodies) == ca.MAX_PROPOSALS
    assert len(out.gap_ids) == ca.MAX_PROPOSALS


def test_run_raises_on_null_output(monkeypatch):
    """When run_agent returns None (or an object whose .output is None),
    run_cost_analyst_agent raises a clear RuntimeError mentioning the null
    output — not a cryptic AttributeError on .output."""
    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry, "run_agent", lambda agent, fn, **k: SimpleNamespace(output=None)
    )

    with pytest.raises(RuntimeError, match="null output"):
        ca.run_cost_analyst_agent(
            settings=Settings(data_dir="/tmp/ca-test"),
            memory="",
            recent_proposals="",
            digest="<aggregate-cost-by-stage>x</aggregate-cost-by-stage>",
        )
