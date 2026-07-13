"""Tests for :mod:`robotsix_mill.agents.standards`.

Covers the fetch-and-cache pipeline for robotsix-standards content
used by the refine stage.  All network I/O is monkeypatched —
no real HTTP requests.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from robotsix_mill.agents.standards import (
    _STANDARDS_CACHE_FILENAME,
    _standards_cache_file,
    fetch_standards_context,
)
from robotsix_mill.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# cache-file path
# ---------------------------------------------------------------------------


def test_cache_file_path(tmp_path: Path):
    s = _settings(tmp_path)
    cf = _standards_cache_file(s)
    assert cf.name == _STANDARDS_CACHE_FILENAME
    assert str(tmp_path) in str(cf)


# ---------------------------------------------------------------------------
# cache hit
# ---------------------------------------------------------------------------


def test_fetch_standards_context_cache_hit(tmp_path: Path, monkeypatch):
    """When the cache file is fresh, no HTTP request is made."""
    s = _settings(tmp_path)
    cf = _standards_cache_file(s)
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text("# Cached standards\n\nSome content.")

    # Ensure the mtime is recent.
    cf.touch()

    http_called = False

    class _FakeClient:
        def __init__(self, **kw):
            nonlocal http_called
            http_called = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            raise AssertionError("should not be called on cache hit")

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    result = fetch_standards_context(s)
    assert "# Cached standards" in result
    assert not http_called


# ---------------------------------------------------------------------------
# stale cache → re-fetch
# ---------------------------------------------------------------------------


def test_fetch_standards_context_stale_cache_refetches(tmp_path: Path, monkeypatch):
    """When the cache is older than the TTL, a fresh fetch is attempted."""
    s = _settings(tmp_path)
    s.web_knowledge_cache_ttl_hours = 1
    cf = _standards_cache_file(s)
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text("# Old standards")

    # Make the cache file appear old.
    old_time = time.time() - 7200  # 2 hours ago
    cf.touch()
    # Monkeypatch stat to fake an old mtime.
    _orig_stat = Path.stat

    class _FakeStat:
        st_mtime = old_time

    monkeypatch.setattr(Path, "stat", lambda self: _FakeStat)

    fetched_urls = []

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            fetched_urls.append(url)
            req = httpx.Request("GET", url)
            resp = httpx.Response(200, text=f"# Fetched: {url}", request=req)
            return resp

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    result = fetch_standards_context(s)
    assert len(fetched_urls) > 0
    # Should contain fetched content, not the stale cache.
    assert "Fetched" in result


# ---------------------------------------------------------------------------
# fetch failure — graceful degradation
# ---------------------------------------------------------------------------


def test_fetch_standards_context_fetch_failure_returns_empty(
    tmp_path: Path, monkeypatch
):
    """When all HTTP fetches fail and no cache exists, return empty string."""
    s = _settings(tmp_path)
    s.web_knowledge_cache_ttl_hours = -1  # force re-fetch

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr(time, "time", lambda: 0)

    result = fetch_standards_context(s)
    assert result == ""


# ---------------------------------------------------------------------------
# injection into refine prompt via run_refine_agent
# ---------------------------------------------------------------------------


def test_run_refine_agent_injects_standards_context(monkeypatch, settings):
    """When ``standards_context`` is non-empty, it appears in the user
    prompt before the title/draft sections."""
    from robotsix_mill.agents import base as base_module
    from robotsix_mill.agents import refining
    from robotsix_mill.agents import retry as retry_module

    run_sync_calls = []

    class _FakeRunResult:
        output = refining.RefineResult(spec_markdown="## Problem\ntest\n")
        _usage = type("_Usage", (), {"requests": 1, "input_tokens": 100})()
        finish_reason = None

        def all_messages(self):
            return []

        def all_messages_json(self):
            return b"{}"

        def new_messages_json(self):
            return b"{}"

        def usage(self):
            return self._usage

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult()

        def close(self):
            pass

    mock_agent = _MockAgent()

    def pass_through_retry(agent, make_run, *, what="model call", sleep=None):
        return make_run(agent)

    monkeypatch.setattr(retry_module, "run_agent", pass_through_retry)
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: mock_agent
    )

    standards_text = "# robotsix-standards\n\nUse towncrier, not commitizen."

    _ = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="Some draft body.",
        standards_context=standards_text,
    )

    assert len(run_sync_calls) == 1
    payload = run_sync_calls[0]
    assert isinstance(payload, str)
    assert "robotsix-standards" in payload
    assert "towncrier" in payload
    assert "commitizen" in payload
    # The standards should appear before the title/draft sections.
    assert payload.index("towncrier") < payload.index("````title")


def test_run_refine_agent_empty_standards_context_notes_unavailable(
    monkeypatch, settings
):
    """When ``standards_context`` is empty, the prompt notes unavailability."""
    from robotsix_mill.agents import base as base_module
    from robotsix_mill.agents import refining
    from robotsix_mill.agents import retry as retry_module

    run_sync_calls = []

    class _FakeRunResult:
        output = refining.RefineResult(spec_markdown="## Problem\ntest\n")
        _usage = type("_Usage", (), {"requests": 1, "input_tokens": 100})()
        finish_reason = None

        def all_messages(self):
            return []

        def all_messages_json(self):
            return b"{}"

        def new_messages_json(self):
            return b"{}"

        def usage(self):
            return self._usage

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult()

        def close(self):
            pass

    mock_agent = _MockAgent()

    def pass_through_retry(agent, make_run, *, what="model call", sleep=None):
        return make_run(agent)

    monkeypatch.setattr(retry_module, "run_agent", pass_through_retry)
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: mock_agent
    )

    _ = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="Some draft body.",
        standards_context="",
    )

    assert len(run_sync_calls) == 1
    payload = run_sync_calls[0]
    assert "Standards context unavailable" in payload
