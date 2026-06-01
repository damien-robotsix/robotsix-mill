"""Claude→DeepSeek model fallback: retry locally, then fall back.

Covers the FallbackAgentHandle wrapper, the run_agent/arun_agent orchestration
(primary's local retries exhausted → fallback), and build_agent's wiring.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from robotsix_mill.agents import base
from robotsix_mill.agents.fallback import FallbackAgentHandle
from robotsix_mill.agents.retry import arun_agent, run_agent
from robotsix_mill.config import Settings


def _settings(**kw) -> Settings:
    return Settings(data_dir=tempfile.mkdtemp(), **kw)


def _noop_sleep(_d):
    return None


class _Handle:
    def __init__(self, name: str, *, fail: bool = False, exc: Exception | None = None):
        self.name = name
        self.fail = fail
        self.exc = exc or RuntimeError("boom")
        self.calls = 0
        self.closed = False

    def run_sync(self, prompt: str, **kw):
        self.calls += 1
        if self.fail:
            raise self.exc
        return f"{self.name}:{prompt}"

    async def run(self, prompt: str, **kw):
        self.calls += 1
        if self.fail:
            raise self.exc
        return f"{self.name}:{prompt}"

    def close(self):
        self.closed = True


# --- FallbackAgentHandle ----------------------------------------------------


def test_handle_delegates_and_builds_fallback_lazily():
    primary, fb = _Handle("primary"), _Handle("deepseek")
    built = {"n": 0}

    def build():
        built["n"] += 1
        return fb

    h = FallbackAgentHandle(primary, build)

    assert h.run_sync("x") == "primary:x"  # delegates to primary
    assert built["n"] == 0  # fallback not built yet
    assert h.build_fallback() is fb
    assert h.build_fallback() is fb  # cached, built once
    assert built["n"] == 1

    h.close()
    assert primary.closed and fb.closed  # closes both (fallback was built)


def test_handle_close_skips_unbuilt_fallback():
    primary = _Handle("primary")
    h = FallbackAgentHandle(primary, lambda: (_ for _ in ()).throw(AssertionError))
    h.close()  # must not build the fallback just to close it
    assert primary.closed


# --- run_agent (sync) -------------------------------------------------------


def test_run_agent_plain_handle_runs_primary():
    h = _Handle("primary")
    out = run_agent(h, lambda x: x.run_sync("hi"), what="t", sleep=_noop_sleep)
    assert out == "primary:hi"
    assert h.calls == 1


def test_run_agent_falls_back_after_primary_terminal_failure():
    primary = _Handle("primary", fail=True, exc=ValueError("dead"))
    fb = _Handle("deepseek")
    h = FallbackAgentHandle(primary, lambda: fb)

    out = run_agent(h, lambda x: x.run_sync("go"), what="t", sleep=_noop_sleep)
    assert out == "deepseek:go"
    assert primary.calls == 1 and fb.calls == 1


def test_run_agent_primary_success_skips_fallback():
    primary = _Handle("primary")
    built = {"n": 0}

    def build():
        built["n"] += 1
        return _Handle("deepseek")

    h = FallbackAgentHandle(primary, build)
    out = run_agent(h, lambda x: x.run_sync("ok"), what="t", sleep=_noop_sleep)
    assert out == "primary:ok"
    assert built["n"] == 0  # fallback never built


# --- arun_agent (async) -----------------------------------------------------


async def test_arun_agent_falls_back_after_primary_terminal_failure():
    primary = _Handle("primary", fail=True, exc=ValueError("dead"))
    fb = _Handle("deepseek")
    h = FallbackAgentHandle(primary, lambda: fb)

    out = await arun_agent(h, lambda x: x.run("go"), what="t")
    assert out == "deepseek:go"
    assert primary.calls == 1 and fb.calls == 1


async def test_arun_agent_plain_handle_runs_primary():
    h = _Handle("primary")
    out = await arun_agent(h, lambda x: x.run("hi"), what="t")
    assert out == "primary:hi"


# --- build_agent wiring -----------------------------------------------------


def _route_claude(monkeypatch, s, *, key, deepseek="DEEPSEEK"):
    monkeypatch.setattr(
        base, "get_secrets", lambda: SimpleNamespace(openrouter_api_key=key)
    )
    monkeypatch.setattr(base, "_build_deepseek_handle", lambda *a, **k: deepseek)
    provider = MagicMock()
    provider.build_agent.side_effect = lambda **kw: "CLAUDE"
    with patch(
        "robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider", return_value=provider
    ):
        return base.build_agent(
            s,
            system_prompt="sys",
            name="refine",
            report_issue=False,
            reply_to_thread=False,
            close_thread=False,
            ask_user=False,
        )


def test_build_agent_wraps_with_fallback_when_key_present(monkeypatch):
    s = _settings(llm_backend="claude_sdk")  # fallback defaults to True
    handle = _route_claude(monkeypatch, s, key="sk-test")
    assert isinstance(handle, FallbackAgentHandle)
    assert handle.build_fallback() == "DEEPSEEK"  # lazy thunk builds DeepSeek


def test_build_agent_no_fallback_when_disabled(monkeypatch):
    s = _settings(llm_backend="claude_sdk", claude_fallback_to_deepseek=False)
    handle = _route_claude(monkeypatch, s, key="sk-test")
    assert not isinstance(handle, FallbackAgentHandle)


def test_build_agent_no_fallback_when_no_openrouter_key(monkeypatch):
    s = _settings(llm_backend="claude_sdk")  # enabled, but no key → skipped
    handle = _route_claude(monkeypatch, s, key=None)
    assert not isinstance(handle, FallbackAgentHandle)


def test_config_default_is_on_and_overridable():
    assert _settings().claude_fallback_to_deepseek is True
    assert (
        _settings(claude_fallback_to_deepseek=False).claude_fallback_to_deepseek
        is False
    )


@pytest.mark.parametrize("flag", [True, False])
def test_config_alias_env(monkeypatch, flag):
    monkeypatch.setenv("MILL_CLAUDE_FALLBACK_TO_DEEPSEEK", "true" if flag else "false")
    assert _settings().claude_fallback_to_deepseek is flag


# NOTE: the real Claude-fail → real DeepSeek-answer path can't run inside this
# hermetic suite (an autouse conftest fixture strips OPENROUTER_API_KEY and
# blocks all network). It is verified out-of-band via scripts/live_fallback.py.
