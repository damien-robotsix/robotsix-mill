"""Tests for the run-health agent — result schema, parallel-list clipping,
and the null-output guard."""

from types import SimpleNamespace

import pytest

from robotsix_mill.agents import run_health as rh
from robotsix_mill.config import Settings


def test_run_health_result_schema():
    r = rh.RunHealthResult(
        draft_titles=["a"], draft_bodies=["b"], gap_ids=["g"], updated_memory="m"
    )
    assert r.draft_titles == ["a"] and r.gap_ids == ["g"]
    # Defaults are empty.
    empty = rh.RunHealthResult()
    assert empty.draft_titles == [] and empty.updated_memory == ""


def test_run_clips_parallel_lists(monkeypatch):
    """run_run_health_agent clips the parallel draft lists to MAX_PROPOSALS
    in lockstep and tolerates ragged lists."""
    n = rh.MAX_PROPOSALS + 5
    fat = rh.RunHealthResult(
        draft_titles=[f"t{i}" for i in range(n)],
        draft_bodies=[f"b{i}" for i in range(n)],
        gap_ids=[f"g{i}" for i in range(n)],
        updated_memory="mem",
    )

    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry, "run_agent", lambda agent, fn, **k: SimpleNamespace(output=fat)
    )

    out = rh.run_run_health_agent(
        settings=Settings(data_dir="/tmp/rh-test"),
        memory="",
        recent_proposals="",
        digest="<run-health-candidates>x</run-health-candidates>",
    )
    assert len(out.draft_titles) == rh.MAX_PROPOSALS
    assert len(out.draft_bodies) == rh.MAX_PROPOSALS
    assert len(out.gap_ids) == rh.MAX_PROPOSALS


def test_run_raises_on_null_output(monkeypatch):
    """When run_agent returns an object whose .output is None,
    run_run_health_agent raises a clear RuntimeError mentioning the null
    output — not a cryptic AttributeError on .output."""
    import robotsix_mill.agents.base as base
    import robotsix_mill.agents.retry as retry

    monkeypatch.setattr(base, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(base, "_safe_close", lambda agent: None)
    monkeypatch.setattr(
        retry, "run_agent", lambda agent, fn, **k: SimpleNamespace(output=None)
    )

    with pytest.raises(RuntimeError, match="null output"):
        rh.run_run_health_agent(
            settings=Settings(data_dir="/tmp/rh-test"),
            memory="",
            recent_proposals="",
            digest="<run-health-candidates>x</run-health-candidates>",
        )
