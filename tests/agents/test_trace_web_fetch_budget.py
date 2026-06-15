"""Tests for per-survey-run web_fetch trace budget.

These tests exercise the trace-level web_fetch budget that spans an
entire survey run (not reset between ask_web_knowledge consults).
"""

from robotsix_mill.agents.web_tools import (
    _cache,
    reset_web_fetch_budget,
    reset_trace_web_fetch_budget,
    make_web_fetch,
)
from robotsix_mill import sandbox
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        _reset_secrets()
        import robotsix_mill.config as _cfg

        _cfg._secrets = Secrets(openrouter_api_key=key)
    return Settings(**env)


class TestTraceWebFetchBudget:
    """Per-survey-run web_fetch budget — trace-level caps that survive
    per-consult budget resets."""

    def test_trace_web_fetch_call_cap(self, tmp_path, monkeypatch):
        """After reset_trace_web_fetch_budget(3, ...), the 4th cache-miss
        web_fetch call returns a budget-exhausted sentinel even after
        reset_web_fetch_budget() (per-consult reset does NOT clear trace
        counters)."""
        _cache.clear()
        reset_web_fetch_budget()
        reset_trace_web_fetch_budget(3, 1_000_000)

        s = _settings(tmp_path, web_fetch_max_calls=100, web_fetch_max_total_bytes=0)

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, f"body for {url}"

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        assert wf("https://x.test/a") == "body for https://x.test/a"
        assert wf("https://x.test/b") == "body for https://x.test/b"
        assert wf("https://x.test/c") == "body for https://x.test/c"
        assert len(calls) == 3

        reset_web_fetch_budget()

        out = wf("https://x.test/d")
        assert "trace budget exhausted" in out.lower()
        assert len(calls) == 3

    def test_trace_web_fetch_byte_cap(self, tmp_path, monkeypatch):
        """Cumulative bytes across multiple consults hit the trace byte
        ceiling."""
        _cache.clear()
        reset_web_fetch_budget()
        reset_trace_web_fetch_budget(100, 500)

        s = _settings(
            tmp_path,
            web_fetch_max_calls=100,
            web_fetch_max_total_bytes=0,
        )

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, "x" * 300

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        assert wf("https://x.test/a") == "x" * 300

        out = wf("https://x.test/b")
        assert "trace budget exhausted" in out.lower()
        assert len(calls) == 2

        reset_web_fetch_budget()
        out = wf("https://x.test/c")
        assert "trace budget exhausted" in out.lower()

    def test_trace_budget_inactive_when_not_set(self, tmp_path, monkeypatch):
        """When reset_trace_web_fetch_budget has never been called (or
        called with max_calls=0), the trace budget is a no-op."""
        _cache.clear()
        reset_web_fetch_budget()
        reset_trace_web_fetch_budget(0, 0)

        s = _settings(tmp_path, web_fetch_max_calls=2, web_fetch_max_total_bytes=0)

        calls: list[str] = []
        monkeypatch.setattr(
            sandbox,
            "fetch",
            lambda url, *, settings: calls.append(url) or (0, f"body for {url}"),
        )
        wf = make_web_fetch(s)

        assert wf("https://x.test/a") == "body for https://x.test/a"
        assert wf("https://x.test/b") == "body for https://x.test/b"
        out = wf("https://x.test/c")
        assert "budget exhausted" in out
        assert "trace" not in out.lower()

    def test_reset_trace_web_fetch_budget_zeroes_counters(self, tmp_path, monkeypatch):
        """Calling reset_trace_web_fetch_budget mid-run zeroes the trace
        counters."""
        _cache.clear()
        reset_web_fetch_budget()
        reset_trace_web_fetch_budget(2, 1_000_000)

        s = _settings(tmp_path, web_fetch_max_calls=100, web_fetch_max_total_bytes=0)

        calls: list[str] = []
        monkeypatch.setattr(
            sandbox,
            "fetch",
            lambda url, *, settings: calls.append(url) or (0, f"body for {url}"),
        )
        wf = make_web_fetch(s)

        assert wf("https://x.test/a") == "body for https://x.test/a"
        assert wf("https://x.test/b") == "body for https://x.test/b"
        out = wf("https://x.test/c")
        assert "trace budget exhausted" in out.lower()

        reset_trace_web_fetch_budget(2, 1_000_000)

        assert wf("https://x.test/d") == "body for https://x.test/d"
        assert wf("https://x.test/e") == "body for https://x.test/e"
