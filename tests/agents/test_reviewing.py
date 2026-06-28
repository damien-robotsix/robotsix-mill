"""Tests for the reviewing agent.

Covers prompt-content semantic anchors (auto-merge eligibility) and
the configurable request-limit plumbing (MILL_REVIEW_REQUEST_LIMIT).
"""

import pytest
from pydantic_ai.usage import UsageLimits

from robotsix_mill.agents.reviewing import (
    SYSTEM_PROMPT,
    ReviewAsk,
    ReviewVerdict,
    run_review_agent,
)
from robotsix_mill.config import Settings, Secrets, _reset_secrets


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
import base64  # noqa: E402

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


def _write_png(tmp_path) -> "object":
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
        # level=3 routes to Claude (vision-capable transport), proving it is
        # the vision *gate* (default False) — not the level — that blocks the
        # attach.
        level=3,
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
        level=3,  # Claude (vision-capable) transport
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
        level=3,  # Claude (vision-capable) transport
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


def test_token_limit_triggers_degraded_retry(tmp_path, monkeypatch):
    """A token-limit error on the first review pass triggers a single
    degraded retry: preseed message_history is dropped and the diff is
    hard-truncated. The degraded pass succeeding yields its verdict."""
    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    # Preseed a real file so the first attempt carries message_history.
    (tmp_path / "x.py").write_text("print('x')\n", encoding="utf-8")

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    big_diff = "diff --git a/x.py b/x.py\n" + ("+line\n" * 50_000)
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

    diff = "diff --git a/x.py b/x.py\n" + ("+line\n" * 500)
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
