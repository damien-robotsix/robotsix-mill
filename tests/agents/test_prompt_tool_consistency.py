"""Guard tests: an agent's FULLY-RESOLVED prompt must not tell the LLM
to *call* a tool that the agent's resolved tool set doesn't include.

Two such mismatches shipped and were point-fixed (PR #755, the refine
sendback override; PR #780, the implement review-feedback injection).
``prompt_tool_consistency`` is the deterministic check that prevents a
third occurrence; these tests pin it AND exercise it against the real
resolved prompts + tool sets of the implement and refine-sendback
agents (covering runtime-injected prompt sections, not just the YAML
``system_prompt``).
"""

from __future__ import annotations

import pydantic_ai
import pytest

from robotsix_mill.agents import base as bmod
from robotsix_mill.agents.prompt_tool_consistency import (
    call_directive_tools,
    unregistered_call_directives,
)
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    # OPENROUTER_API_KEY is now a Secrets-only field; pop before Settings()
    env.pop("OPENROUTER_API_KEY", None)
    return Settings(**env)


# ── Pure-function unit tests ──────────────────────────────────────────


def test_call_directive_requires_parens():
    """A bare backtick mention with no following ``(`` is not a call
    directive — the disclaimer guard from the ticket."""
    assert call_directive_tools("you do not have a `close_thread` tool") == set()
    assert call_directive_tools("call `close_thread(comment_id)`") == {"close_thread"}


def test_disclaimer_phrasing_not_flagged():
    """'you do not have a `close_thread` tool' must NOT be flagged even
    when the agent genuinely lacks the tool."""
    prompt = (
        "Closing review threads is the reviewer's responsibility — you do "
        "not have a `close_thread` tool."
    )
    assert unregistered_call_directives(prompt, resolved_tools=set()) == set()


def test_call_directive_for_missing_tool_flagged():
    """A ``call `<tool>(...)``` directive naming an absent tool is flagged."""
    prompt = "For each comment, call `close_thread(comment_id)` once resolved."
    assert unregistered_call_directives(prompt, resolved_tools={"reply_to_thread"}) == {
        "close_thread"
    }


def test_call_directive_for_present_tool_ok():
    """A call directive naming a present tool is not flagged."""
    prompt = "call `reply_to_thread(thread_id, body)` to explain your approach."
    assert (
        unregistered_call_directives(prompt, resolved_tools={"reply_to_thread"})
        == set()
    )


def test_rst_double_backtick_directive_matched():
    """RST ``double-backtick`` call directives (as in the refine sendback
    prompt) are matched too."""
    prompt = "If fully addressed: call ``close_thread(comment_id)``."
    assert unregistered_call_directives(prompt, resolved_tools=set()) == {
        "close_thread"
    }


def test_known_tools_filter_suppresses_non_tool_calls():
    """A parenthesised backtick span that is not a known mill tool
    (e.g. ``cast(...)``) is ignored when a catalog is supplied."""
    prompt = "use `cast(x)` to coerce the value"
    assert (
        unregistered_call_directives(
            prompt, resolved_tools=set(), known_tools={"close_thread"}
        )
        == set()
    )
    # Without the catalog, the parens pattern alone would flag it.
    assert unregistered_call_directives(prompt, resolved_tools=set()) == {"cast"}


# ── build_agent wiring: the guard fires at construction time ──────────


def test_build_agent_raises_on_call_directive_to_absent_tool(tmp_path, monkeypatch):
    """``build_agent`` invokes the consistency check before constructing
    the pydantic-ai Agent: a prompt that tells the agent to *call* a
    known mill tool it wasn't wired with raises ``ValueError`` at build
    time (not at runtime)."""
    from robotsix_mill.agents.base import build_agent
    from robotsix_mill.agents.close_thread import make_close_thread_tool

    s = _settings(tmp_path)
    # Ensure ``close_thread`` is in the registry catalog (registration
    # happens when the factory runs), then build an agent that omits it.
    make_close_thread_tool(s, "x")

    with pytest.raises(ValueError, match="close_thread"):
        build_agent(
            s,
            system_prompt="For each comment, call `close_thread(comment_id)`.",
            close_thread=False,
        )


def test_build_agent_allows_disclaimer_mention(tmp_path, monkeypatch):
    """A bare backtick mention (not a call directive) for a tool the
    agent lacks does NOT trip the guard — build proceeds past the
    check to the backend construction."""
    from robotsix_mill.agents import base

    s = _settings(tmp_path)
    # Stop right after the guard so we don't need a real backend. Default
    # level (2) resolves to the DeepSeek transport, so patching the
    # DeepSeek handle builder is enough.
    sentinel = object()
    monkeypatch.setattr(base, "_build_deepseek_handle", lambda *a, **k: sentinel)

    handle = base.build_agent(
        s,
        system_prompt="You do not have a `close_thread` tool.",
        close_thread=False,
    )
    assert handle is sentinel


# ── Capture harness for the real agent build paths ────────────────────


def _capture(monkeypatch, output_obj):
    """Patch the model/provider seam so building+running an agent stays
    in-process, and capture the resolved system prompt, resolved tool
    names, and the user prompt(s) passed to ``run_sync``."""
    captured: dict = {}

    class FakeResponse:
        finish_reason = "stop"

    class FakeResult:
        def __init__(self, out):
            self.output = out
            self.response = FakeResponse()

        def all_messages(self):
            return []

        def all_messages_json(self):
            return b"[]"

        def new_messages_json(self):
            return b"[]"

    class FakeAgent:
        def __init__(self, **kw):
            captured["system_prompt"] = kw.get("system_prompt")
            captured["tools"] = sorted(t.__name__ for t in (kw.get("tools") or []))

        def run_sync(self, prompt, *, usage_limits=None, message_history=None, **kw):
            captured.setdefault("user_prompts", []).append(prompt)
            return FakeResult(output_obj)

        def close(self):
            pass

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod, "new_deepseek_model", lambda model_name, level: (object(), object())
    )
    # Force the DeepSeek (pydantic-ai) provider for ALL levels so the
    # FakeAgent above intercepts the build. refine is level 3 (Claude SDK)
    # which would otherwise bypass pydantic_ai.Agent and spawn a real
    # subprocess. Tool injection — what these tests assert — is
    # provider-independent, so this keeps the check meaningful while
    # staying hermetic.  We monkeypatch the Claude SDK provider prefix
    # so build_agent never takes the Claude path.
    monkeypatch.setattr(bmod, "_CLAUDE_SDK_PROVIDER", "__nonexistent__")
    return captured


def _combined_prompt(captured: dict) -> str:
    return (
        (captured.get("system_prompt") or "")
        + "\n\n"
        + "\n\n".join(captured.get("user_prompts") or [])
    )


def _capture_implement_review(monkeypatch, tmp_path):
    from robotsix_mill.agents.coordinating import ImplementResult, run_coordinator

    s = _settings(tmp_path)
    captured = _capture(monkeypatch, ImplementResult(summary="ok"))
    run_coordinator(
        settings=s,
        repo_dir=tmp_path,
        spec="Do the thing.",
        feedback="[REVIEW id=7 @ src/x.py] Please address this comment.",
    )
    return captured


def _capture_refine_sendback(monkeypatch, tmp_path):
    from robotsix_mill.agents.refining import RefineResult, run_refine_agent

    s = _settings(tmp_path)
    captured = _capture(
        monkeypatch, RefineResult(spec_markdown="ok", updated_memory="")
    )
    run_refine_agent(
        settings=s,
        title="A ticket",
        draft="A draft to refine.",
        repo_dir=tmp_path,
        reviewer_comments="[id=42 @ spec] Please clarify the acceptance criteria.",
    )
    return captured


# ── Regression tests against the real resolved prompts ────────────────


def test_implement_review_feedback_prompt_is_consistent(tmp_path, monkeypatch):
    """The implement agent's resolved prompt (static system_prompt +
    runtime-injected review-feedback section) must not call any tool it
    lacks. Guards against reintroducing the PR #780 `close_thread`
    injection while implement.yaml still sets ``close_thread: false``."""
    cap = _capture_implement_review(monkeypatch, tmp_path)
    resolved = set(cap["tools"])
    # The review-feedback section is what was injected at runtime.
    combined = _combined_prompt(cap)
    assert "review-feedback" in combined
    # implement has reply_to_thread but NOT close_thread.
    assert "reply_to_thread" in resolved
    assert "close_thread" not in resolved

    catalog = _known_catalog(monkeypatch, tmp_path)
    assert (
        unregistered_call_directives(combined, resolved, known_tools=catalog) == set()
    )


def test_refine_sendback_prompt_is_consistent(tmp_path, monkeypatch):
    """The refine sendback agent's resolved prompt
    (``REVIEWER_SENDBACK_PROMPT`` + injected reviewer-feedback) calls
    ``reply_to_thread``/``close_thread``; the sendback overrides MUST
    wire both into the tool set. Guards against reintroducing the
    PR #755 override omission."""
    cap = _capture_refine_sendback(monkeypatch, tmp_path)
    resolved = set(cap["tools"])
    combined = _combined_prompt(cap)
    # The sendback prompt legitimately issues both call directives.
    assert "close_thread(comment_id)" in combined
    assert "reply_to_thread(thread_id, body)" in combined
    # ...and the overrides must have wired both tools in.
    assert {"reply_to_thread", "close_thread"} <= resolved

    catalog = _known_catalog(monkeypatch, tmp_path)
    assert (
        unregistered_call_directives(combined, resolved, known_tools=catalog) == set()
    )


def _known_catalog(monkeypatch, tmp_path) -> set[str]:
    """The catalog of real mill tool names — union of the implement and
    refine-sendback resolved tool sets (so ``close_thread`` is present
    even though implement lacks it)."""
    impl = set(_capture_implement_review(monkeypatch, tmp_path)["tools"])
    refine = set(_capture_refine_sendback(monkeypatch, tmp_path)["tools"])
    return impl | refine


# ── Negative controls: the check WOULD fail if either bug returned ────


def test_reintroducing_implement_close_thread_injection_is_flagged(
    tmp_path, monkeypatch
):
    """If the PR #780 directive ('call `close_thread(comment_id)`') were
    re-added to the implement review-feedback text, the check flags it,
    because implement.yaml keeps ``close_thread: false``."""
    cap = _capture_implement_review(monkeypatch, tmp_path)
    resolved = set(cap["tools"])
    catalog = _known_catalog(monkeypatch, tmp_path)
    buggy = (
        _combined_prompt(cap)
        + "\nFor each comment, call `close_thread(comment_id)` once resolved."
    )
    assert unregistered_call_directives(buggy, resolved, known_tools=catalog) == {
        "close_thread"
    }


def test_reintroducing_refine_sendback_override_omission_is_flagged(
    tmp_path, monkeypatch
):
    """If the PR #755 sendback overrides were dropped, ``close_thread``
    would leave the resolved tool set while the prompt still calls it —
    the check flags exactly that."""
    cap = _capture_refine_sendback(monkeypatch, tmp_path)
    combined = _combined_prompt(cap)
    catalog = _known_catalog(monkeypatch, tmp_path)
    # Simulate the override omission: close_thread no longer wired in.
    resolved_without_override = set(cap["tools"]) - {"close_thread"}
    assert unregistered_call_directives(
        combined, resolved_without_override, known_tools=catalog
    ) == {"close_thread"}
