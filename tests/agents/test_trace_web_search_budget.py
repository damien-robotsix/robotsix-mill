"""Tests for per-survey-run web_search trace budget."""

import asyncio
from robotsix_mill.agents.web_knowledge import (
    reset_trace_web_search_budget,
    _make_tools,
)
from robotsix_mill.config import Settings


def _settings(tmp_path):
    return Settings(data_dir=str(tmp_path))


class TestTraceWebSearchBudget:
    """Per-survey-run web_search budget."""

    def test_trace_web_search_cap(self, tmp_path, monkeypatch):
        """After reset_trace_web_search_budget(2), the 3rd web_search
        call returns a budget-exhausted sentinel."""
        s = _settings(tmp_path)

        async def fake_run_web_research(*, settings, query):
            return f"conclusion for: {query}"

        # The web_search closure does a lazy import of web_research
        # inside _make_tools, so we must monkeypatch the web_research
        # module itself, not web_knowledge.
        import robotsix_mill.agents.web_research as wr_mod

        monkeypatch.setattr(wr_mod, "run_web_research", fake_run_web_research)

        reset_trace_web_search_budget(2)
        tools = _make_tools(s)
        web_search = tools[-1]

        r1 = asyncio.run(web_search("query 1"))
        r2 = asyncio.run(web_search("query 2"))
        assert "conclusion for: query 1" == r1
        assert "conclusion for: query 2" == r2

        r3 = asyncio.run(web_search("query 3"))
        assert "trace budget exhausted" in r3.lower()

    def test_trace_web_search_inactive_when_not_set(self, tmp_path, monkeypatch):
        """reset_trace_web_search_budget(0) → no-op."""
        s = _settings(tmp_path)

        async def fake_run_web_research(*, settings, query):
            return f"ok: {query}"

        import robotsix_mill.agents.web_research as wr_mod

        monkeypatch.setattr(wr_mod, "run_web_research", fake_run_web_research)

        reset_trace_web_search_budget(0)
        tools = _make_tools(s)
        web_search = tools[-1]

        for i in range(10):
            r = asyncio.run(web_search(f"query {i}"))
            assert f"ok: query {i}" == r
