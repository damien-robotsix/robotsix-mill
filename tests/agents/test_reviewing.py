"""Tests for the reviewing agent.

Covers prompt-content semantic anchors (auto-merge eligibility) and
the configurable request-limit plumbing (MILL_REVIEW_REQUEST_LIMIT).
"""

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.usage import UsageLimits

from robotsix_mill.agents.reviewing import (
    SYSTEM_PROMPT,
    ReviewAsk,
    ReviewVerdict,
    _is_finish_reason_error,
    _is_output_token_exhaustion,
    _is_token_limit_error,
    changed_line_ranges_from_diff,
    run_review_agent,
)
from robotsix_mill.config import Secrets, Settings, _reset_secrets

# ------------------------------------------------------------------
# Field description — semantic anchors
# ------------------------------------------------------------------


def test_auto_merge_eligible_description_approve_true():
    """Field description anchors: APPROVE + minor/informational observations → true."""
    desc = ReviewVerdict.model_fields["auto_merge_eligible"].description
    assert desc is not None
    desc_lower = desc.lower()
    assert "approve" in desc_lower
    assert "minor or informational observations" in desc_lower
    assert "set to true" in desc_lower


def test_auto_merge_eligible_description_named_reason_to_false():
    """Field description anchors: false requires a genuine security risk or correctness blocker."""
    desc = ReviewVerdict.model_fields["auto_merge_eligible"].description
    assert desc is not None
    desc_lower = desc.lower()
    assert "genuine security risk" in desc_lower
    assert "correctness blocker" in desc_lower
    assert "set to false" in desc_lower


def test_auto_merge_eligible_description_request_changes_false():
    """Field description: REQUEST_CHANGES / NEEDS_DISCUSSION → false."""
    desc = ReviewVerdict.model_fields["auto_merge_eligible"].description
    assert desc is not None
    desc_lower = desc.lower()
    assert "request_changes" in desc_lower
    assert "needs_discussion" in desc_lower
    assert "set to false only when" in desc_lower


# ------------------------------------------------------------------
# SYSTEM_PROMPT — semantic anchors
# ------------------------------------------------------------------
# Normalise whitespace so assertions aren't tripped up by multi-line
# prose that wraps long lines at ~80 cols.


@pytest.fixture
def prompt() -> str:
    """SYSTEM_PROMPT lowercased with newlines collapsed to spaces."""
    return SYSTEM_PROMPT.lower().replace("\n", " ")


def test_system_prompt_approve_no_concern_true(prompt):
    """SYSTEM_PROMPT: APPROVE + no concern raised → true."""
    assert "approve" in prompt
    assert "raised no" in prompt
    assert "specific concern" in prompt
    assert "a human doesn't need to look" in prompt


def test_system_prompt_false_requires_articulable_reason(prompt):
    """SYSTEM_PROMPT: false only with articulable, specific reason."""
    assert "articulate a *specific* reason" in prompt
    assert "human should still look" in prompt
    assert "set this to ``false`` only when" in prompt


def test_system_prompt_request_changes_needs_discussion_false(prompt):
    """SYSTEM_PROMPT: REQUEST_CHANGES / NEEDS_DISCUSSION always false."""
    assert "request_changes" in prompt
    assert "needs_discussion" in prompt
    assert "always leave this" in prompt
    assert "``false``" in prompt


def test_system_prompt_tie_breaker_human_judgment_concern(prompt):
    """SYSTEM_PROMPT: tie-breaker re-aimed at human-judgment concern,
    not change-size."""
    assert "when unsure whether a genuine human-judgment concern" in prompt
    assert "default to ``false``" in prompt

    # The old size-based criteria must be GONE.
    assert "small and focused" not in prompt
    assert "single concern, few files" not in prompt
    assert "zero risk of regression" not in prompt
    assert "no new infrastructure" not in prompt


# ------------------------------------------------------------------
# Pydantic default unchanged
# ------------------------------------------------------------------


def test_auto_merge_eligible_default_is_false():
    """Pydantic default=False must be preserved — the prompt bias is what
    changes operational behaviour, not the structural fallback."""
    assert ReviewVerdict.model_fields["auto_merge_eligible"].default is False


# ------------------------------------------------------------------
# Request-limit config knob
# ------------------------------------------------------------------


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    # Mirror openrouter_api_key into Secrets so get_secrets() works
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    # OPENROUTER_API_KEY is now a Secrets-only field; pop before Settings()
    env.pop("OPENROUTER_API_KEY", None)
    return Settings(**env)


class _FakeAgentResult:
    def __init__(self, output):
        self.output = output


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def run_sync(self, prompt, *, usage_limits=None, **kwargs):
        self.calls.append((prompt, usage_limits, kwargs))
        return _FakeAgentResult(
            ReviewVerdict(
                verdict="APPROVE",
                comments="lgtm",
                auto_merge_eligible=False,
            )
        )


def _patch_agent(monkeypatch, agent):
    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", lambda *a, **k: agent)


def _patch_agent_definition(monkeypatch, agent):
    """Patch the higher-level builder so the Claude-SDK routing branch
    (which would import claude_agent_sdk) is bypassed entirely."""
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        lambda *a, **k: agent,
    )


# A minimal but valid 1x1 PNG (content is irrelevant — the code only
# reads the bytes and wraps them in a BinaryContent).
import base64

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_request_limit_from_settings_not_hardcoded(tmp_path, monkeypatch):
    """The review agent's UsageLimits(request_limit=…) must come from
    settings.review_request_limit, NOT a hard-coded integer."""

    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        review_request_limit="42",
    )

    result = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x",
        spec="Fix x",
    )
    assert isinstance(result, ReviewVerdict)
    assert result.verdict == "APPROVE"

    assert len(agent.calls) == 1
    _, usage_limits, kwargs = agent.calls[0]

    # Must be a UsageLimits object, never a bare request_limit= kwarg.
    assert isinstance(usage_limits, UsageLimits)
    assert "request_limit" not in kwargs
    assert usage_limits.request_limit == 42  # from settings, not 20


# --- Board screenshot attachment (Tier 3) -----------------------------------


def _write_png(tmp_path) -> object:
    from pathlib import Path

    p = Path(tmp_path) / "board.png"
    p.write_bytes(_PNG_1X1)
    return p


def test_screenshot_not_attached_when_vision_gate_off(tmp_path, monkeypatch):
    """Regression for the 1200s stall (ticket 565a / 348e): routed to the
    Claude SDK backend with the vision capability gate at its default
    (False), an existing board PNG must NOT be attached — the run input
    stays a bare ``str`` with no BinaryContent, so the input shape that
    hangs the llmio bridge can no longer be emitted. The transport-level
    fix (teaching the robotsix-llmio claude_sdk bridge to consume image
    parts) lives there and needs a dependency bump — out of scope here."""
    from pydantic_ai import BinaryContent

    agent = _FakeAgent()
    _patch_agent_definition(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    png = _write_png(tmp_path)

    result = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x",
        spec="Fix x",
        # level=4 routes to Claude (vision-capable transport), proving it is
        # the vision *gate* (default False) — not the level — that blocks the
        # attach.
        level=4,
        screenshot_path=png,
    )
    assert isinstance(result, ReviewVerdict)

    assert len(agent.calls) == 1
    run_input = agent.calls[0][0]
    assert isinstance(run_input, str)
    assert not isinstance(run_input, list)
    assert "Fix x" in run_input
    # No BinaryContent leaked into the string path.
    assert BinaryContent.__name__ not in run_input


def test_screenshot_attached_when_vision_gate_on(tmp_path, monkeypatch):
    """Claude SDK backend + ``claude_sdk_vision_enabled=True`` + an
    existing PNG → the run input is a list whose final element is a
    BinaryContent image, alongside the diff/spec text. This exercises the
    (future) vision-enabled path that the capability gate guards."""
    from pydantic_ai import BinaryContent

    agent = _FakeAgent()
    _patch_agent_definition(monkeypatch, agent)

    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        claude_sdk_vision_enabled=True,
    )
    png = _write_png(tmp_path)

    result = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x",
        spec="Fix x",
        level=4,  # Claude (vision-capable) transport
        screenshot_path=png,
    )
    assert isinstance(result, ReviewVerdict)

    assert len(agent.calls) == 1
    run_input = agent.calls[0][0]
    assert isinstance(run_input, list)
    images = [c for c in run_input if isinstance(c, BinaryContent)]
    assert len(images) == 1
    assert images[0].media_type == "image/png"
    assert images[0].data == _PNG_1X1
    # The diff/spec text is still present alongside the image.
    assert any(isinstance(c, str) and "Fix x" in c for c in run_input)


def test_screenshot_not_attached_on_deepseek_path(tmp_path, monkeypatch):
    """Default DeepSeek backend → NO image is attached; the run input is the
    bare string prompt, byte-for-byte equivalent to today."""
    from pydantic_ai import BinaryContent

    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")  # default llm_backend
    png = _write_png(tmp_path)

    result = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x",
        spec="Fix x",
        screenshot_path=png,
    )
    assert isinstance(result, ReviewVerdict)

    assert len(agent.calls) == 1
    run_input = agent.calls[0][0]
    assert isinstance(run_input, str)
    assert not isinstance(run_input, list)
    assert "Fix x" in run_input
    # Sanity: no BinaryContent leaked into the string path.
    assert BinaryContent.__name__ not in run_input


def test_missing_screenshot_falls_back_to_text(tmp_path, monkeypatch):
    """Claude SDK routing + vision gate ON but the screenshot file does
    not exist → no crash, falls back to the bare-string text path. The
    missing/unreadable-file silent degradation must stay intact."""
    from pathlib import Path

    agent = _FakeAgent()
    _patch_agent_definition(monkeypatch, agent)

    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        claude_sdk_vision_enabled=True,
    )

    result = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x",
        spec="Fix x",
        level=4,  # Claude (vision-capable) transport
        screenshot_path=Path(tmp_path) / "does-not-exist.png",
    )
    assert isinstance(result, ReviewVerdict)
    run_input = agent.calls[0][0]
    assert isinstance(run_input, str)


# --- _coerce_verdict: parse-fallback must not crash the review stage --------


def test_coerce_verdict_passthrough():
    from robotsix_mill.agents.reviewing import ReviewVerdict, _coerce_verdict

    v = ReviewVerdict(verdict="APPROVE", comments="ok")
    assert _coerce_verdict(v) is v


def test_coerce_verdict_str_degrades_to_needs_discussion():
    # 402b crash: review agent returned a bare str, the stage did
    # verdict.verdict -> AttributeError -> Fatal BLOCK. Degrade to
    # NEEDS_DISCUSSION (never APPROVE — must not auto-merge unreviewed code).
    from robotsix_mill.agents.reviewing import ReviewVerdict, _coerce_verdict

    v = _coerce_verdict("raw model text, not JSON")
    assert isinstance(v, ReviewVerdict)
    assert v.verdict == "NEEDS_DISCUSSION"
    assert v.auto_merge_eligible is False
    assert "could not be parsed" in v.comments


def test_coerce_verdict_none_degrades():
    from robotsix_mill.agents.reviewing import _coerce_verdict

    assert _coerce_verdict(None).verdict == "NEEDS_DISCUSSION"


# --- Shared structured-output guard: re-prompt before terminal coercion -----


class _StubAgentRunResult:
    def __init__(self, output):
        self.output = output

    def all_messages(self):
        return []


def test_run_review_agent_reprompts_once_on_unstructured_output(tmp_path, monkeypatch):
    """When the first call returns a raw 12K-char string, the shared
    guard re-prompts once via ``run_agent``; the structured second-call
    result is returned, ``_coerce_verdict`` is NOT engaged."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    calls: list[str] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        calls.append(what)
        if len(calls) == 1:
            return _StubAgentRunResult("x" * 12_000)
        return _StubAgentRunResult(
            ReviewVerdict(
                verdict="APPROVE",
                comments="lgtm",
                auto_merge_eligible=False,
            )
        )

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x",
        spec="Fix x",
    )
    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "APPROVE"
    assert len(calls) == 2
    assert calls[0] == "review"
    assert "re-prompt" in calls[1]


def test_run_review_agent_degrades_to_needs_discussion_after_two_failures(
    tmp_path, monkeypatch
):
    """Two consecutive raw-string returns: the shared guard re-prompts
    once, the re-prompt also returns raw text, ``_coerce_verdict``
    degrades the final answer to NEEDS_DISCUSSION. ``run_agent`` is
    called exactly twice (initial + one re-prompt)."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    calls: list[str] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        calls.append(what)
        return _StubAgentRunResult("x" * 12_000)

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x",
        spec="Fix x",
    )
    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "NEEDS_DISCUSSION"
    assert verdict.auto_merge_eligible is False
    assert len(calls) == 2


# --- Token-limit / context-window degraded retry ----------------------------


_TOKEN_LIMIT_MSG = (
    "Model token limit 1048576 exceeded: 1500815 tokens requested "
    "(1491808 input text). maximum context length is 1048576."
)

_OUTPUT_EXHAUSTION_MSG = (
    "Model token limit (8192) exceeded before any response was generated."
)

# A body OpenRouter sends back on a provider failure: the upstream 5xx'd
# or rate-limited, surfaced as finish_reason='error' rather than a token
# or content signal. pydantic-ai wraps the raw response JSON in
# UnexpectedModelBehavior.body.
_FINISH_REASON_ERROR_BODY = (
    '{"choices": [{"finish_reason": "error", '
    '"message": {"content": "maximum context length exceeded"}}]}'
)


def test_is_finish_reason_error_true_for_unexpected_model_behavior_body():
    """finish_reason='error' in the wrapped response body is detected."""
    exc = UnexpectedModelBehavior(
        "Exceeded maximum output retries (4)",
        body=_FINISH_REASON_ERROR_BODY,
    )
    assert _is_finish_reason_error(exc) is True


def test_is_finish_reason_error_true_for_message_only():
    """finish_reason='error' in the exception message alone is detected.

    Regression guard: a future pydantic-ai update may map
    finish_reason='error' to a different exception type WITHOUT a
    ``body`` attribute — the message-based check must still classify it
    so the handlers keep skipping the retry tiers.
    """
    exc = RuntimeError("OpenRouter provider failure: finish_reason='error'")
    assert _is_finish_reason_error(exc) is True


def test_is_finish_reason_error_false_for_other_finish_reasons():
    """A normal finish reason (or none) is not a provider error."""
    exc = UnexpectedModelBehavior(
        "Exceeded maximum output retries (4)",
        body='{"choices": [{"finish_reason": "length"}]}',
    )
    assert _is_finish_reason_error(exc) is False
    assert _is_finish_reason_error(RuntimeError("some unrelated boom")) is False


def test_finish_reason_error_not_classified_as_token_limit_or_exhaustion():
    """A provider finish_reason='error' is NOT a token-limit /
    output-exhaustion signal — even when the wrapped body echoes
    context-length phrases that would otherwise match."""
    exc = UnexpectedModelBehavior(
        "Model token limit 1048576 exceeded: maximum context length is 1048576.",
        body=_FINISH_REASON_ERROR_BODY,
    )
    assert _is_finish_reason_error(exc) is True
    assert _is_token_limit_error(exc) is False
    assert _is_output_token_exhaustion(exc) is False


def test_token_limit_classifiers_still_work_without_finish_reason_error():
    """The finish_reason guard must not degrade existing classification."""
    exc = UnexpectedModelBehavior(
        _TOKEN_LIMIT_MSG,
        body='{"choices": [{"finish_reason": "length", '
        '"message": {"content": "maximum context length exceeded"}}]}',
    )
    assert _is_finish_reason_error(exc) is False
    assert _is_token_limit_error(exc) is True


def test_finish_reason_error_skips_degraded_retry(tmp_path, monkeypatch):
    """finish_reason='error' on the first review pass is surfaced as
    NEEDS_DISCUSSION immediately — no output-exhaustion retry, no
    chunked review, no degraded single-pass burns model budget on a
    provider outage."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    calls: list[str] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        calls.append(what)
        raise UnexpectedModelBehavior(
            "Exceeded maximum output retries (4)",
            body=_FINISH_REASON_ERROR_BODY,
        )

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x\n" + ("+x\n" * 100),
        spec="Fix x",
    )

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "NEEDS_DISCUSSION"
    assert verdict.auto_merge_eligible is False
    assert "finish_reason='error'" in verdict.comments
    # Exactly one attempt — the provider failure is never retried
    # within this attempt.
    assert len(calls) == 1


def test_token_limit_triggers_degraded_retry(tmp_path, monkeypatch):
    """A token-limit error on the first review pass triggers a single
    degraded retry: preseed message_history is dropped and the diff is
    hard-truncated. The degraded pass succeeding yields its verdict."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    # Preseed a real file so the first attempt carries message_history.
    (tmp_path / "x.py").write_text("print('x')\n", encoding="utf-8")

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    big_diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1,50001 @@\n"
        "-print('x')\n" + ("+line\n" * 50_000)
    )
    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        # Populate agent.calls with the prompt/kwargs for this attempt.
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        if len(seen) == 1:
            raise RuntimeError(_TOKEN_LIMIT_MSG)
        return _StubAgentRunResult(
            ReviewVerdict(verdict="APPROVE", comments="ok on truncated diff")
        )

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff=big_diff,
        spec="Fix x",
        repo_dir=tmp_path,
        reference_files=["x.py"],
    )

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "APPROVE"
    assert len(seen) == 2

    # First attempt carries preseed message_history (prompt is None).
    assert "message_history" in seen[0]["kwargs"]
    # Degraded retry drops preseed and passes a string prompt with the
    # truncation note, and the diff it carries is much smaller.
    assert "message_history" not in seen[1]["kwargs"]
    degraded_prompt = seen[1]["prompt"]
    assert isinstance(degraded_prompt, str)
    assert "heavily truncated" in degraded_prompt
    assert len(degraded_prompt) < len(big_diff)


def test_token_limit_persists_yields_needs_discussion(tmp_path, monkeypatch):
    """When the degraded retry ALSO hits a token-limit error, the stage
    must not crash: a best-effort NEEDS_DISCUSSION verdict is returned
    whose comment explains the truncation."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    calls: list[str] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        calls.append(what)
        raise RuntimeError(_TOKEN_LIMIT_MSG)

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x\n" + ("+x\n" * 1000),
        spec="Fix x",
    )

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "NEEDS_DISCUSSION"
    assert verdict.auto_merge_eligible is False
    assert "context" in verdict.comments.lower()
    # Initial attempt + one degraded retry.
    assert len(calls) == 2


def test_non_token_exception_propagates(tmp_path, monkeypatch):
    """A non-token-limit exception is NOT swallowed by the degraded path —
    it propagates exactly as before (one run_agent call, no retry)."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    calls: list[str] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        calls.append(what)
        raise RuntimeError("some unrelated boom")

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    with pytest.raises(RuntimeError, match="some unrelated boom"):
        run_review_agent(settings=s, diff="diff --git a/x b/x", spec="Fix x")
    assert len(calls) == 1


# --- Output-token exhaustion (max_tokens too low for reasoning output) -----


def test_output_exhaustion_retries_with_higher_max_tokens(tmp_path, monkeypatch):
    """Output-token exhaustion on first review triggers a retry with
    increased max_tokens (same untruncated diff, same preseed)."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    # Preseed a real file so the first attempt carries message_history.
    (tmp_path / "x.py").write_text("print('x')\n", encoding="utf-8")

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1,501 @@\n"
        "-print('x')\n" + ("+line\n" * 500)
    )
    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        if len(seen) == 1:
            raise RuntimeError(_OUTPUT_EXHAUSTION_MSG)
        return _StubAgentRunResult(
            ReviewVerdict(verdict="APPROVE", comments="ok with bigger budget")
        )

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff=diff,
        spec="Fix x",
        repo_dir=tmp_path,
        reference_files=["x.py"],
    )

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "APPROVE"
    assert len(seen) == 2

    # First attempt carries preseed message_history.
    assert "message_history" in seen[0]["kwargs"]
    # Second attempt is the output-exhaustion retry: same preseed, higher
    # max_tokens, diff NOT truncated.
    assert "message_history" in seen[1]["kwargs"]
    assert "model_settings" in seen[1]["kwargs"]
    assert seen[1]["kwargs"]["model_settings"]["max_tokens"] == 65536
    # Retry preserves preseed, so prompt is None (message_history used).
    assert seen[1]["prompt"] is None


def test_output_exhaustion_persists_yields_needs_discussion(tmp_path, monkeypatch):
    """When output-token exhaustion persists after the budget-increase
    retry, return NEEDS_DISCUSSION with 'output' (not 'context window')
    in the comment."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    calls: list[str] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        calls.append(what)
        raise RuntimeError(_OUTPUT_EXHAUSTION_MSG)

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff="diff --git a/x b/x\n" + ("+x\n" * 100),
        spec="Fix x",
    )

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "NEEDS_DISCUSSION"
    assert verdict.auto_merge_eligible is False
    assert "output" in verdict.comments.lower()
    assert "context window" not in verdict.comments.lower()
    # Initial attempt + one output-exhaustion retry (no truncation path).
    assert len(calls) == 2


# ------------------------------------------------------------------
# extra_roots forwarding
# ------------------------------------------------------------------


def test_extra_roots_forwarded_to_build_fs_tools(tmp_path, monkeypatch):
    """``extra_roots`` is forwarded to ``build_fs_tools`` when provided."""
    from robotsix_mill.agents import fs_tools

    captured: list = []

    def fake_build_fs_tools(
        root, settings, *, pre_seeded=None, extra_roots=None, sandbox_image=None
    ):
        captured.append(extra_roots)
        return []

    monkeypatch.setattr(fs_tools, "build_fs_tools", fake_build_fs_tools)

    agent = _FakeAgent()
    _patch_agent_definition(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    extra = [tmp_path / "other"]

    run_review_agent(
        settings=s,
        diff="diff",
        spec="spec",
        repo_dir=repo_dir,
        extra_roots=extra,
    )
    assert captured == [extra]


def test_extra_roots_defaults_to_none(tmp_path, monkeypatch):
    """When ``extra_roots`` is not passed, ``build_fs_tools`` receives ``None``."""
    from robotsix_mill.agents import fs_tools

    captured: list = []

    def fake_build_fs_tools(
        root, settings, *, pre_seeded=None, extra_roots=None, sandbox_image=None
    ):
        captured.append(extra_roots)
        return []

    monkeypatch.setattr(fs_tools, "build_fs_tools", fake_build_fs_tools)

    agent = _FakeAgent()
    _patch_agent_definition(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    run_review_agent(
        settings=s,
        diff="diff",
        spec="spec",
        repo_dir=repo_dir,
    )
    assert captured == [None]


# --- Chunked review (Tier 2 degradation) ------------------------------------


def _multi_file_diff(files: list[tuple[str, str]]) -> str:
    """Build a unified git diff string from *files* (``[(path, body), …]``)."""
    parts: list[str] = []
    for path, body in files:
        parts.append(
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{body.count(chr(10)) + 1} @@\n"
            f"{body}\n"
        )
    return "".join(parts)


def test_chunked_review_synthesizes_verdicts(tmp_path, monkeypatch):
    """Multi-file diff where the first pass token-limits, all per-chunk
    reviews succeed, and the synthesis pass produces a consolidated
    APPROVE verdict with the ``[Chunked review: …]`` marker."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    diff = _multi_file_diff(
        [
            ("a.py", "+line\n" * 30),
            ("b.py", "+line\n" * 30),
            ("c.py", "+line\n" * 30),
        ]
    )

    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        if len(seen) == 1:
            raise RuntimeError(_TOKEN_LIMIT_MSG)
        # Per-chunk and synthesis calls all return APPROVE.
        return _StubAgentRunResult(ReviewVerdict(verdict="APPROVE", comments="lgtm"))

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(settings=s, diff=diff, spec="Fix things")

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "APPROVE"
    # 1 failed + 3 chunk + 1 synthesis = 5
    assert len(seen) == 5

    # Chunked-review marker must be present on the final verdict.
    assert verdict.comments.startswith(
        "[Chunked review: 3 files reviewed in 3 chunks due to diff size]"
    )

    # Per-chunk calls (indices 1, 2, 3) must carry use_preseed=False and
    # a note naming "part X of 3".
    for i, expected_part in enumerate((1, 2, 3), start=1):
        call_kwargs = seen[i]["kwargs"]
        # message_history must NOT be present (use_preseed=False).
        assert "message_history" not in call_kwargs, f"chunk {i} had preseed"
        prompt = seen[i]["prompt"]
        assert isinstance(prompt, str)
        assert f"part {expected_part} of 3" in prompt, f"chunk {i} missing part note"

    # Synthesis call (index 4) must carry a synthesis note.
    synthesis_prompt = seen[4]["prompt"]
    assert isinstance(synthesis_prompt, str)
    assert "Synthesis pass" in synthesis_prompt
    assert "previously reviewed 3 files" in synthesis_prompt


def test_chunked_review_single_oversized_file_falls_through(tmp_path, monkeypatch):
    """Single-file diff whose chunk exceeds the per-file budget → chunked
    review returns None → degraded single-pass → NEEDS_DISCUSSION (the
    existing surrender message, NOT the chunked-review marker)."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    # Override the per-chunk budget to a tiny value so the single-file
    # chunk easily exceeds it.  The chunked-review guard uses
    # max(review_diff_max_chars, 40_000); setting it to 1000 means
    # max(1000, 40_000) = 40_000, which is still too large for the test
    # to trigger the guard.  We need the diff to be > 40_000 chars.
    # So we set review_diff_max_chars to 0 (uncapped) — the guard
    # becomes max(0, 40_000) = 40_000.  A ~50 KB diff exceeds that.
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        review_diff_max_chars="0",
    )

    big_body = "+" + "x" * 49_000 + "\n"
    diff = _multi_file_diff([("huge.py", big_body)])

    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        if len(seen) == 1:
            raise RuntimeError(_TOKEN_LIMIT_MSG)
        # Degraded retry also token-limits.
        raise RuntimeError(_TOKEN_LIMIT_MSG)

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(settings=s, diff=diff, spec="Fix huge")

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "NEEDS_DISCUSSION"
    assert verdict.auto_merge_eligible is False
    # The existing surrender message, NOT the chunked-review marker.
    assert "context window" in verdict.comments.lower()
    assert not verdict.comments.startswith("[Chunked review:")
    # Exactly 2 calls: Tier 1 (token-limit) → chunked review bails
    # (single oversized file) → Tier 3 (token-limit again) → surrender.
    assert len(seen) == 2


def test_chunked_review_synthesis_token_limit_falls_through(tmp_path, monkeypatch):
    """A token-limit raised by the SYNTHESIS pass must not crash the
    stage: chunked review returns None and the runner falls through to
    Tier 3 (degraded single-pass), preserving the graceful-degradation
    contract."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    diff = _multi_file_diff(
        [
            ("a.py", "+line\n" * 30),
            ("b.py", "+line\n" * 30),
        ]
    )

    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        # Call 1: Tier 1 full pass → token limit.
        if len(seen) == 1:
            raise RuntimeError(_TOKEN_LIMIT_MSG)
        # Calls 2-3: per-chunk reviews succeed.
        if len(seen) <= 3:
            return _StubAgentRunResult(
                ReviewVerdict(verdict="APPROVE", comments="lgtm")
            )
        # Call 4: synthesis pass → token limit (output exhaustion can
        # fire regardless of prompt size).
        if len(seen) == 4:
            raise RuntimeError(_TOKEN_LIMIT_MSG)
        # Call 5: Tier 3 degraded single-pass succeeds.
        return _StubAgentRunResult(
            ReviewVerdict(verdict="APPROVE", comments="tier3 ok")
        )

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(settings=s, diff=diff, spec="Fix things")

    assert isinstance(verdict, ReviewVerdict)
    # Tier 3 verdict, not a crash and not the chunked marker.
    assert verdict.verdict == "APPROVE"
    assert verdict.comments == "tier3 ok"
    assert not verdict.comments.startswith("[Chunked review:")
    # 1 full + 2 chunks + 1 synthesis + 1 tier-3 = 5 calls.
    assert len(seen) == 5


def test_chunked_review_request_changes_floor(tmp_path, monkeypatch):
    """If any chunk verdict is REQUEST_CHANGES, the synthesized verdict
    is floored at REQUEST_CHANGES (the LLM cannot silently drop it), the
    dropped asks are unioned in, and auto_merge_eligible is forced False
    in chunked mode."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    diff = _multi_file_diff(
        [
            ("a.py", "+line\n" * 30),
            ("b.py", "+line\n" * 30),
        ]
    )

    ask = ReviewAsk(
        title="Fix the bug",
        description="a.py introduces an off-by-one",
        files=["a.py"],
    )

    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        if len(seen) == 1:
            raise RuntimeError(_TOKEN_LIMIT_MSG)
        # Chunk 1 (a.py) demands changes.
        if len(seen) == 2:
            return _StubAgentRunResult(
                ReviewVerdict(
                    verdict="REQUEST_CHANGES",
                    comments="off-by-one in a.py",
                    request_changes=[ask],
                )
            )
        # Chunk 2 approves; synthesis (wrongly) approves and claims
        # auto-merge eligibility.
        return _StubAgentRunResult(
            ReviewVerdict(verdict="APPROVE", comments="lgtm", auto_merge_eligible=True)
        )

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(settings=s, diff=diff, spec="Fix things")

    assert isinstance(verdict, ReviewVerdict)
    assert verdict.verdict == "REQUEST_CHANGES"
    assert any(a.title == "Fix the bug" for a in verdict.request_changes)
    assert verdict.auto_merge_eligible is False
    assert verdict.comments.startswith("[Chunked review: 2 files")


# --- changed_line_ranges_from_diff (preseed excerpt inputs) ------------------


def _modified_diff(
    path: str, old_start: int, old_count: int, new_start: int, new_count: int
) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n"
        f" context\n"
        f"-old\n"
        f"+new\n"
    )


def test_changed_line_ranges_modified_files():
    diff = _modified_diff("a.py", 10, 4, 20, 4)
    assert changed_line_ranges_from_diff(diff) == {"a.py": [(20, 23)]}


def test_changed_line_ranges_skips_new_and_deleted_files():
    new_file = (
        "diff --git a/new.py b/new.py\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+one\n"
        "+two\n"
    )
    deleted = (
        "diff --git a/gone.py b/gone.py\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,5 +0,0 @@\n"
        "-one\n"
        "-two\n"
    )
    modified = _modified_diff("mod.py", 1, 2, 1, 2)
    ranges = changed_line_ranges_from_diff(new_file + deleted + modified)
    assert ranges == {"mod.py": [(1, 2)]}


def test_changed_line_ranges_multiple_hunks_same_file():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,2 @@\n"
        " ctx\n"
        "-a\n"
        "+b\n"
        "@@ -50,3 +60,3 @@\n"
        " ctx\n"
        "-c\n"
        "+d\n"
    )
    assert changed_line_ranges_from_diff(diff) == {"x.py": [(1, 2), (60, 62)]}


# --- Preseed excerpts (bounded review context) -------------------------------


def test_preseed_preloads_excerpts_not_whole_file(tmp_path, monkeypatch):
    """For a modified file, the review preseed carries only the changed
    region plus ``review_preseed_context_lines`` of context — not the
    whole file — and the read_file tool-call args reflect the excerpt
    range so a later on-demand full read is still allowed."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "mod.py").write_text(
        "".join(f"line{i}\n" for i in range(100)), encoding="utf-8"
    )

    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        review_preseed_context_lines="2",
    )

    diff = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -40,2 +40,2 @@\n"
        " line38\n"
        "-line39\n"
        "+line39x\n"
        "-line40\n"
        "+line40x\n"
        " line41\n"
    )

    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        return _StubAgentRunResult(ReviewVerdict(verdict="APPROVE", comments="lgtm"))

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff=diff,
        spec="Fix mod",
        repo_dir=repo_dir,
        reference_files=["mod.py"],
    )

    assert verdict.verdict == "APPROVE"
    assert len(seen) == 1

    history = seen[0]["kwargs"]["message_history"]
    # history = [ModelRequest(prompt), ModelResponse(calls), ModelRequest(returns)]
    assert len(history) == 3
    calls_msg, returns_msg = history[1], history[2]

    assert len(calls_msg.parts) == 1
    assert len(returns_msg.parts) == 1

    call_part = calls_msg.parts[0]
    assert call_part.args_as_dict() == {
        "path": "mod.py",
        "offset": 38,
        "limit": 6,
    }

    content = returns_msg.parts[0].content
    assert "[preload excerpt: mod.py lines 38-43 of 100]" in content
    assert "line37" in content
    assert "line42" in content
    assert "line0" not in content
    assert "line99" not in content


def test_preseed_skips_new_files(tmp_path, monkeypatch):
    """A new file's full content is already in the diff, so it is NOT
    preloaded again — the run keeps the plain string prompt and no
    message_history is attached."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "new.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    diff = (
        "diff --git a/new.py b/new.py\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+one\n"
        "+two\n"
        "+three\n"
    )

    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        return _StubAgentRunResult(ReviewVerdict(verdict="APPROVE", comments="lgtm"))

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    run_review_agent(
        settings=s,
        diff=diff,
        spec="Add new",
        repo_dir=repo_dir,
        reference_files=["new.py"],
    )

    assert len(seen) == 1
    assert "message_history" not in seen[0]["kwargs"]
    assert isinstance(seen[0]["prompt"], str)


def test_preseed_uses_provided_ranges_when_diff_is_truncated(tmp_path, monkeypatch):
    """The preseed uses *changed_line_ranges* supplied by the caller even
    when the *diff* passed in is middle-truncated and no longer carries
    the file's hunks — a modified file dropped from the bounded diff still
    gets a bounded excerpt (no review-coverage regression)."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "mod.py").write_text(
        "".join(f"line{i}\n" for i in range(100)), encoding="utf-8"
    )

    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        review_preseed_context_lines="1",
    )

    # A truncated diff: mod.py's hunks are entirely absent (dropped from
    # the middle by head_tail_keep), but the caller still knows its changed
    # ranges because it derived them from the UNBOUNDED diff.
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "\n[... git-diff truncated: 1234 chars omitted from the middle ...]\n\n"
        "diff --git a/z.py b/z.py\n"
        "--- a/z.py\n"
        "+++ b/z.py\n"
        "@@ -1 +1 @@\n"
        "-z\n"
        "+y\n"
    )
    changed_line_ranges = {"mod.py": [(40, 43)]}

    seen: list[dict] = []

    def fake_run_agent(agent, make_run, *, what, **kw):
        make_run(agent)
        prompt, _limits, kwargs = agent.calls[-1]
        seen.append({"prompt": prompt, "kwargs": kwargs})
        return _StubAgentRunResult(ReviewVerdict(verdict="APPROVE", comments="lgtm"))

    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", fake_run_agent)

    verdict = run_review_agent(
        settings=s,
        diff=diff,
        spec="Fix mod",
        repo_dir=repo_dir,
        reference_files=["mod.py"],
        changed_line_ranges=changed_line_ranges,
    )

    assert verdict.verdict == "APPROVE"
    assert len(seen) == 1

    history = seen[0]["kwargs"]["message_history"]
    assert len(history) == 3
    calls_msg, returns_msg = history[1], history[2]
    assert len(calls_msg.parts) == 1
    assert len(returns_msg.parts) == 1

    call_part = calls_msg.parts[0]
    assert call_part.args_as_dict() == {
        "path": "mod.py",
        "offset": 39,
        "limit": 6,
    }

    content = returns_msg.parts[0].content
    assert "[preload excerpt: mod.py lines 39-44 of 100]" in content
    assert "line38" in content
    assert "line43" in content
    assert "line37" not in content
    assert "line44" not in content


# ------------------------------------------------------------------
# _repo_conventions — release-please repos have no changelog fragment
# ------------------------------------------------------------------


class TestRepoConventions:
    """The reviewer must not demand a fragment mill itself deletes.

    On a release-please repo the implement stage folds the fragment's kind
    into the commit subject and then deletes the file (``drop_fragments``),
    so it is never in the diff. Reviewers kept reporting that absence as an
    unmet deliverable, implement kept re-creating the file, and the pair
    span until the 10/10 implement-review ceiling — the block that held
    auto-mail 590f and central-deploy de52.
    """

    def test_none_repo_dir_yields_no_conventions(self):
        from robotsix_mill.agents.reviewing import _repo_conventions

        assert _repo_conventions(None) == ""

    def test_towncrier_repo_yields_no_conventions(self, tmp_path):
        from robotsix_mill.agents.reviewing import _repo_conventions

        (tmp_path / "pyproject.toml").write_text("[tool.towncrier]\n")
        assert _repo_conventions(tmp_path) == ""

    def test_release_please_repo_tells_reviewer_to_expect_no_fragment(self, tmp_path):
        from robotsix_mill.agents.reviewing import _repo_conventions

        (tmp_path / "release-please-config.json").write_text("{}")
        text = _repo_conventions(tmp_path)

        assert "release-please" in text
        assert "changelog fragment" in text
        # The instruction must survive a stale spec that still asks for one.
        assert "ticket-spec" in text
        assert "Do NOT report" in text
