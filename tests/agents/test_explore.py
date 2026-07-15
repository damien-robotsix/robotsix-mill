"""The read-only exploration sub-agent."""

import asyncio
import contextlib

from robotsix_mill.agents import explore
from robotsix_mill.agents.explore import make_explore_tool
from robotsix_mill.config import Settings, Secrets, _reset_secrets


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


def test_no_key_degrades_not_raises(tmp_path):
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = asyncio.run(
        explore.run_explore(settings=s, repo_dir=tmp_path, question="where is X?")
    )
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_missing_repo_degrades_not_raises(tmp_path):
    """When repo_dir does not exist, run_explore returns an
    'explore unavailable' message without importing pydantic_ai or
    making any HTTP call."""
    missing = tmp_path / "nonexistent"
    s = _settings(tmp_path, OPENROUTER_API_KEY="valid-key")
    out = asyncio.run(
        explore.run_explore(settings=s, repo_dir=missing, question="where is X?")
    )
    assert "explore unavailable" in out
    assert "workspace repo directory does not exist" in out
    assert "not been cloned yet" in out


def test_parallel_explore_fans_out_labeled(tmp_path, monkeypatch):
    """parallel_explore batches questions into a single run_explore
    call and returns every answer labeled by question."""
    s = _settings(tmp_path)

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        return f"ANS:{question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    out = asyncio.run(tool(["q1", "q2", "q3"]))
    # All three question labels appear in the output.
    assert "[1] q1" in out
    assert "[2] q2" in out
    assert "[3] q3" in out
    # The batched question text is visible in the answer body
    # (the fake echoes its prompt), and each original question
    # appears inside the batched prompt.
    assert "q1" in out and "q2" in out and "q3" in out


def test_parallel_explore_single_question_no_batching(tmp_path, monkeypatch):
    """A single question is delegated directly (no batch wrapper)."""
    s = _settings(tmp_path)

    seen = {}

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        seen["question"] = question
        return f"ANS:{question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    out = asyncio.run(tool(["just-one"]))
    assert "[1] just-one" in out
    assert "ANS:just-one" in out
    # The question is passed verbatim — no batch wrapper.
    assert seen["question"] == "just-one"


def test_parallel_explore_batches_into_single_call(tmp_path, monkeypatch):
    """Multiple questions are batched into a single run_explore call
    (not fanned out concurrently), so the system prompt is sent once."""
    s = _settings(tmp_path)
    seen = {"calls": 0, "questions": []}

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        seen["calls"] += 1
        seen["questions"].append(question)
        return "answer"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    asyncio.run(tool([f"q{i}" for i in range(5)]))
    # Exactly one call for all five questions (batched).
    assert seen["calls"] == 1
    # The single call's prompt contains every question.
    prompt = seen["questions"][0]
    for i in range(5):
        assert f"q{i}" in prompt


def test_parallel_explore_surface_failure(tmp_path, monkeypatch):
    """When the single batched run_explore call raises, the failure is
    surfaced as an error string while question labels are preserved."""
    s = _settings(tmp_path)

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        raise RuntimeError("batch failed")

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    out = asyncio.run(tool(["a", "b", "c"]))
    # Question labels still appear in the output.
    assert "[1] a" in out
    assert "[2] b" in out
    assert "[3] c" in out
    # The failure is captured.
    assert "explore failed" in out
    assert "batch failed" in out


def test_parallel_explore_empty_questions(tmp_path):
    s = _settings(tmp_path)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    assert "no questions" in asyncio.run(tool([]))


def test_parallel_explore_batch_cap_rejects_over_limit(tmp_path, monkeypatch):
    """More than _PARALLEL_EXPLORE_BATCH_CAP questions returns an
    error asking the caller to split the batch."""
    s = _settings(tmp_path)

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        return "should-not-be-called"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    cap = explore._PARALLEL_EXPLORE_BATCH_CAP
    out = asyncio.run(tool([f"q{i}" for i in range(cap + 1)]))
    assert "at most" in out
    assert str(cap) in out
    assert "Split into smaller batches" in out
    assert "should-not-be-called" not in out


def test_parallel_explore_grep_prefilter_short_circuits(tmp_path, monkeypatch):
    """When git grep finds ≤ _GREP_PREFILTER_MAX_LINES matches for a
    question, the answer is returned directly — no scout call."""
    s = _settings(tmp_path)

    # Create a real git repo with a file containing a known term.
    import subprocess

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    (tmp_path / "sample.py").write_text("def migrate_config(x):\n    return x + 1\n")
    subprocess.run(
        ["git", "add", "sample.py"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    seen = {"calls": 0}

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        seen["calls"] += 1
        return "scout-answer"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)

    # Question with a quoted term that grep can find.
    out = asyncio.run(tool(["where is 'migrate_config' defined?"]))
    assert "grep pre-filter" in out
    assert "migrate_config" in out
    # The scout was never called.
    assert seen["calls"] == 0


def test_parallel_explore_grep_prefilter_falls_through_on_no_match(
    tmp_path,
    monkeypatch,
):
    """When git grep finds nothing, the question falls through to the
    full scout."""
    s = _settings(tmp_path)
    import subprocess

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # Create an empty commit so git grep works (needs a valid HEAD).
    subprocess.run(
        ["git", "commit", "-m", "init", "--allow-empty"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )

    seen = {"calls": 0}

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        seen["calls"] += 1
        return "scout-answer"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)

    out = asyncio.run(tool(["where is 'nonexistent_symbol' defined?"]))
    assert "scout-answer" in out
    assert "grep pre-filter" not in out
    assert seen["calls"] == 1


def test_system_prompt_forbids_whole_file_shell_dumps():
    """The explore system prompt closes the two run_command escape
    hatches flagged in trace review: shelling out to dump whole files,
    and issuing redundant overlapping discovery commands."""
    sp = explore._SYSTEM_PROMPT.lower()
    # No whole-file shell dumps via run_command — redirect to read_file.
    assert "run_command" in sp
    assert "cat" in sp and "head" in sp and "tail" in sp
    assert "read_file" in sp
    # Consolidate / avoid redundant discovery commands.
    assert "consolidate" in sp or "overlapping" in sp
    assert "re-run" in sp


def test_system_prompt_instructs_merge_adjacent_read_ranges():
    """The explore system prompt tells the scout to merge adjacent
    read ranges into a single read_file call, with a concrete example
    drawn from observed trace waste (two sequential reads of the same
    file that should have been one)."""
    sp = explore._SYSTEM_PROMPT.lower()
    assert "merge" in sp or "adjacent" in sp
    assert "single read" in sp or "maximum" in sp
    # Concrete merge example from the trace
    assert "offset=20, limit=120" in sp


def test_system_prompt_warns_against_re_reading_already_held_ranges():
    """The explore system prompt tells the scout that read_file refuses
    any partial slice whose line range (or a subset) it already holds,
    returning no new content — a wasted turn.  The scout must track
    read ranges and scroll back instead of re-issuing."""
    sp = explore._SYSTEM_PROMPT.lower()
    assert "never re-read" in sp or "never re-issue" in sp
    assert "already read this answer" in sp
    assert "no new content" in sp
    assert "subset" in sp


def test_repo_scoped_explore_unknown_repo(tmp_path):
    """A repo-scoped explore call naming an unregistered repo returns a
    helpful error listing the valid ids — never raises, never explores."""
    s = _settings(tmp_path)
    tool = explore.make_repo_scoped_explore_tool(s, {"repo-a": tmp_path / "a"})
    out = asyncio.run(tool("repo-z", "where is X?"))
    assert "unknown repo" in out
    assert "repo-a" in out


def test_repo_scoped_explore_routes_to_selected_repo(tmp_path, monkeypatch):
    """The selected repo determines the scout's ``repo_dir`` and the
    ``extra_roots`` are confined to that one clone (no mill-bias)."""
    s = _settings(tmp_path)
    seen = {}

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        seen["dir"] = repo_dir
        seen["extra_roots"] = extra_roots
        return f"OK {question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    tool = explore.make_repo_scoped_explore_tool(s, {"repo-a": a, "repo-b": b})
    assert (
        asyncio.run(tool("repo-b", "where is the worker?")) == "OK where is the worker?"
    )
    assert seen["dir"] == b
    assert seen["extra_roots"] == [b]


def test_tool_delegates_to_seam(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        seen["q"] = question
        seen["dir"] = repo_dir
        seen["extra_roots"] = extra_roots
        return f"FOUND: {question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = make_explore_tool(s, tmp_path)
    assert asyncio.run(tool("where is the worker?")) == "FOUND: where is the worker?"
    assert seen["q"] == "where is the worker?" and seen["dir"] == tmp_path
    assert seen["extra_roots"] is None


def test_explore_tool_runs_inside_an_active_event_loop(tmp_path, monkeypatch):
    """Regression: under the Claude SDK backend the explore tool callback
    fires INSIDE the SDK's already-running event loop. The old sync tool
    called ``run_sync`` → ``asyncio.run`` there, raising "this event loop
    is already running" (caught and degraded to "explore failed: …"), so
    the coordinator never got an answer. The tool must be a coroutine fn
    that awaits its seam, composing with whatever loop is driving it."""
    import inspect

    s = _settings(tmp_path)

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        return f"OK: {question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = make_explore_tool(s, tmp_path)
    assert inspect.iscoroutinefunction(tool), "explore tool must be async"

    async def driver():
        # We are now on a running loop — exactly like the SDK tool callback.
        return await tool("where is the worker?")

    assert asyncio.run(driver()) == "OK: where is the worker?"


def _patch_explore_model(monkeypatch, cap):
    """Patch the level-1 model seam (base.build_openrouter_model) so the
    explore sub-agent builds nothing real. Captures the resolved model name."""
    from robotsix_mill.agents import base as bmod
    from robotsix_llmio.core.factory import default_tier_config

    class FakeModel:
        def __init__(self, name):
            cap["model"] = name

    def fake_build_openrouter_model(level=1, *, online=False):
        # explore builds a level-1 (flash) DeepSeek model; resolve it the
        # same way base does so the captured name reflects the real binding.
        model_name = default_tier_config().for_level(level).model_name
        if online:
            model_name = f"{model_name}:online"
        return FakeModel(model_name), object()

    monkeypatch.setattr(bmod, "build_openrouter_model", fake_build_openrouter_model)


def test_explore_subagent_is_read_only_and_uses_flash_model(tmp_path, monkeypatch):
    """The sub-agent gets ONLY read_file/list_dir/run_command (never
    write_file/edit_file/delete_file) and runs on the cheap level-1 (flash)
    model, bounded."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        explore_request_limit="7",
        explore_max_tokens="600",
    )
    cap = {}

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in kw.get("tools", []))
            cap["name"] = kw.get("name")
            cap["model_settings"] = kw.get("model_settings")

        async def run(self, q, *, usage_limits=None):
            cap["limit"] = usage_limits.request_limit
            return type("R", (), {"output": "answer"})()

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, cap)

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "answer"
    # level-1 (flash) DeepSeek model — resolved from llmio's tier defaults.
    assert cap["model"] == "deepseek/deepseek-v4-flash"
    assert cap["tools"] == [
        "list_dir",
        "read_file",
        "run_command",
    ]  # NO write/edit/delete
    assert cap["limit"] == 7
    assert cap["name"] == "explore"
    # model_settings with max_tokens is wired
    ms = cap["model_settings"]
    assert ms is not None
    assert ms["max_tokens"] == 600


def test_known_context_is_prepended_to_prompt(tmp_path, monkeypatch):
    """When known_context is non-empty, the prompt handed to agent.run
    contains both the known-context text and the original question."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    cap = {}

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, q, *, usage_limits=None):
            cap["prompt"] = q
            return type("R", (), {"output": "answer"})()

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, cap)

    out = asyncio.run(
        explore.run_explore(
            settings=s,
            repo_dir=tmp_path,
            question="where is tracing?",
            known_context="src/robotsix_mill/runtime/tracing.py already read",
        )
    )
    assert out == "answer"
    assert "src/robotsix_mill/runtime/tracing.py already read" in cap["prompt"]
    assert "where is tracing?" in cap["prompt"]
    assert "Known context" in cap["prompt"]


def test_prompt_unchanged_when_known_context_omitted(tmp_path, monkeypatch):
    """When known_context is omitted, the prompt equals the original
    question verbatim (no wrapper)."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    cap = {}

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, q, *, usage_limits=None):
            cap["prompt"] = q
            return type("R", (), {"output": "answer"})()

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, cap)

    out = asyncio.run(
        explore.run_explore(settings=s, repo_dir=tmp_path, question="where is X?")
    )
    assert out == "answer"
    assert cap["prompt"] == "where is X?"


def _patch_fake_agent(monkeypatch, cap):
    """Patch the explore Agent + model seams to capture the prompt."""

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, q, *, usage_limits=None):
            cap["prompt"] = q
            return type("R", (), {"output": "answer"})()

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, cap)


def test_pre_seeded_paths_are_prepended_to_prompt(tmp_path, monkeypatch):
    """pre_seeded_paths injects a <preloaded_files> block listing each
    path plus a do-not-re-read instruction, and keeps the question."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    cap = {}
    _patch_fake_agent(monkeypatch, cap)

    out = asyncio.run(
        explore.run_explore(
            settings=s,
            repo_dir=tmp_path,
            question="where is tracing?",
            pre_seeded_paths=["a.py", "b.py"],
        )
    )
    assert out == "answer"
    assert "<preloaded_files>" in cap["prompt"]
    assert "a.py" in cap["prompt"]
    assert "b.py" in cap["prompt"]
    assert "Do NOT spend tokens" in cap["prompt"]
    assert "where is tracing?" in cap["prompt"]


def test_pre_seeded_paths_merge_with_known_context(tmp_path, monkeypatch):
    """When both known_context and pre_seeded_paths are supplied, both
    appear in the composed prompt (neither overwrites the other)."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    cap = {}
    _patch_fake_agent(monkeypatch, cap)

    out = asyncio.run(
        explore.run_explore(
            settings=s,
            repo_dir=tmp_path,
            question="where is X?",
            known_context="some terse facts",
            pre_seeded_paths=["model.py"],
        )
    )
    assert out == "answer"
    assert "some terse facts" in cap["prompt"]
    assert "model.py" in cap["prompt"]
    assert "<preloaded_files>" in cap["prompt"]
    assert "where is X?" in cap["prompt"]


def test_prompt_unchanged_when_pre_seeded_paths_omitted(tmp_path, monkeypatch):
    """With neither known_context nor pre_seeded_paths, the prompt equals
    the verbatim question (no behavior change)."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    cap = {}
    _patch_fake_agent(monkeypatch, cap)

    out = asyncio.run(
        explore.run_explore(settings=s, repo_dir=tmp_path, question="where is X?")
    )
    assert out == "answer"
    assert cap["prompt"] == "where is X?"


def test_make_explore_tool_forwards_pre_seeded_paths(tmp_path, monkeypatch):
    """The make_explore_tool closure forwards pre_seeded_paths to
    run_explore."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    cap = {}

    async def fake_run_explore(**kw):
        cap.update(kw)
        return "answer"

    monkeypatch.setattr(explore, "run_explore", fake_run_explore)

    tool = explore.make_explore_tool(
        s, tmp_path, pre_seeded_paths=["model.py", "provider.py"]
    )
    out = asyncio.run(tool("where is X?"))
    assert out == "answer"
    assert cap["pre_seeded_paths"] == ["model.py", "provider.py"]


# --- bounded retry + sentinel tests -------------------------------------


class _FakeUsageLimitExceeded(Exception):
    pass


_FakeUsageLimitExceeded.__name__ = "UsageLimitExceeded"


def test_explore_retries_once_with_stricter_prompt(tmp_path, monkeypatch):
    """When the primary explore call raises UsageLimitExceeded, the
    bounded retry kicks in with a stricter no-tools prompt and
    request_limit=2.  If the retry succeeds, its answer is returned."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        explore_request_limit="20",
    )

    primary_agent_calls = []
    retry_agent_calls = []

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")
            self._tools = kw.get("tools", [])
            self._system_prompt = kw.get("system_prompt", "")
            if self._name == "explore-retry":
                retry_agent_calls.append(
                    dict(
                        name=self._name,
                        tools=self._tools,
                        system_prompt=self._system_prompt,
                    )
                )

        async def run(self, q, *, usage_limits=None):
            if self._name == "explore":
                primary_agent_calls.append(1)
                raise _FakeUsageLimitExceeded("budget cap")
            # explore-retry succeeds
            return type("R", (), {"output": "retry-answer"})()

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        pydantic_ai.exceptions, "UsageLimitExceeded", _FakeUsageLimitExceeded
    )
    _patch_explore_model(monkeypatch, {})

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "retry-answer"
    assert len(primary_agent_calls) == 1
    assert len(retry_agent_calls) == 1
    # Retry agent must have NO tools
    assert retry_agent_calls[0]["tools"] == []
    # Retry agent's system prompt must mention budget and "unable to answer"
    sp = retry_agent_calls[0]["system_prompt"]
    assert "budget" in sp.lower() or "limit" in sp.lower()
    assert "unable to answer" in sp


def test_explore_sentinel_set_on_double_failure(tmp_path, monkeypatch):
    """When both the primary explore call AND the bounded retry raise
    UsageLimitExceeded, is_explore_budget_exhausted() returns True."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        explore_request_limit="20",
    )

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")

        async def run(self, q, *, usage_limits=None):
            raise _FakeUsageLimitExceeded("budget cap")

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        pydantic_ai.exceptions, "UsageLimitExceeded", _FakeUsageLimitExceeded
    )
    _patch_explore_model(monkeypatch, {})

    # Reset sentinel before test
    explore.reset_explore_budget_exhausted()
    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert "explore failed" in out
    assert explore.is_explore_budget_exhausted() is True
    # Reset after test
    explore.reset_explore_budget_exhausted()
    assert explore.is_explore_budget_exhausted() is False


def test_explore_sentinel_reset_clears_state(tmp_path, monkeypatch):
    """reset_explore_budget_exhausted() clears the sentinel."""
    explore.mark_explore_budget_exhausted()
    assert explore.is_explore_budget_exhausted() is True
    explore.reset_explore_budget_exhausted()
    assert explore.is_explore_budget_exhausted() is False


def test_explore_max_tokens_validator_rejects_zero_or_negative():
    """The config validator rejects explore_max_tokens < 1."""
    from pathlib import Path

    from pydantic import ValidationError
    import pytest

    with pytest.raises(ValidationError) as exc_info:
        _settings(Path("."), explore_max_tokens="0")
    assert "Input should be greater than or equal to 1" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info:
        _settings(Path("."), explore_max_tokens="-1")
    assert "Input should be greater than or equal to 1" in str(exc_info.value)


# --- trace_stage child-span tests ---------------------------------------


def test_trace_stage_explore_nests_under_parent(tmp_path, monkeypatch):
    """run_explore opens a child span named 'explore' via trace_stage."""
    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(explore, "trace_stage", fake_trace_stage)
    _patch_explore_model(monkeypatch, {})

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, q, *, usage_limits=None):
            return type("R", (), {"output": "answer"})()

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    (tmp_path / "a.txt").write_text("hi")

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "answer"
    assert spans == ["explore"]


def test_trace_stage_parallel_explore_nests_under_parent(tmp_path, monkeypatch):
    """parallel_explore opens a child span named 'parallel_explore' via
    trace_stage."""
    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(explore, "trace_stage", fake_trace_stage)

    async def fake_run_explore(*, settings, repo_dir, question, extra_roots=None):
        return f"ANS:{question}"

    monkeypatch.setattr(explore, "run_explore", fake_run_explore)
    s = _settings(tmp_path)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    out = asyncio.run(tool(["q1", "q2"]))
    # The batched prompt contains both questions.
    assert "q1" in out and "q2" in out
    assert "parallel_explore" in spans
    # The single inner explore call also opens its own "explore" span,
    # but we've monkeypatched run_explore away — inner spans are not
    # recorded here. We only verify the outer wrapper.


# --- finish_reason == 'length' continuation tests ------------------------


def test_continuation_passes_message_history_on_length(tmp_path, monkeypatch):
    """When finish_reason == 'length', the continuation agent.run receives
    message_history=result.all_messages() and the final output is the
    concatenation of both runs joined by a newline."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap: dict = {}

    # Pre-construct the fake message list that all_messages() will return
    fake_messages = [{"role": "user", "content": "the original prompt"}]

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")
            cap.setdefault("agent_names", []).append(self._name)

        async def run(self, q, *, usage_limits=None, message_history=None):
            if self._name != "explore-retry":
                cap.setdefault("runs", []).append(
                    {"prompt": q, "message_history": message_history}
                )
            if len(cap.get("runs", [])) == 1:
                # First run: return truncated output with finish_reason == 'length'
                r = type("R", (), {})()
                r.output = "first truncated"
                r.response = type("Resp", (), {"finish_reason": "length"})()
                r.all_messages = lambda: fake_messages
                return r
            else:
                # Continuation run
                r = type("R", (), {})()
                r.output = "continuation answer"
                r.response = type("Resp", (), {"finish_reason": "stop"})()
                return r

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, {})

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "first truncated\ncontinuation answer"
    assert len(cap["runs"]) == 2
    # The continuation call must have received message_history
    assert cap["runs"][1]["message_history"] is not None
    assert cap["runs"][1]["message_history"] == fake_messages


def test_continuation_falls_back_when_all_messages_unavailable(tmp_path, monkeypatch):
    """When result.all_messages() raises AttributeError, the continuation
    still runs but without message_history (graceful degradation)."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap: dict = {}

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")

        async def run(self, q, *, usage_limits=None, message_history=None):
            if self._name != "explore-retry":
                cap.setdefault("runs", []).append(
                    {"prompt": q, "message_history": message_history}
                )
            if len(cap.get("runs", [])) == 1:
                # First run: result with NO all_messages()
                r = type("R", (), {})()
                r.output = "first truncated"
                r.response = type("Resp", (), {"finish_reason": "length"})()
                # no all_messages — will raise AttributeError
                return r
            else:
                r = type("R", (), {})()
                r.output = "continuation answer"
                return r

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, {})

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "first truncated\ncontinuation answer"
    assert len(cap["runs"]) == 2
    # The continuation call must NOT have received message_history (graceful fallback)
    assert cap["runs"][1]["message_history"] is None


def test_no_continuation_when_finish_reason_is_not_length(tmp_path, monkeypatch):
    """When finish_reason is 'stop', no continuation call is made and the
    single output is returned unchanged.  No AttributeError either."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap: dict = {}

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")

        async def run(self, q, *, usage_limits=None, message_history=None):
            if self._name != "explore-retry":
                cap.setdefault("runs", []).append(q)
            r = type("R", (), {})()
            r.output = "complete answer"
            r.response = type("Resp", (), {"finish_reason": "stop"})()
            return r

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, {})

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "complete answer"
    # Only one run — no continuation
    assert len(cap["runs"]) == 1


def test_no_continuation_when_response_is_none(tmp_path, monkeypatch):
    """When result.response is None (missing), no continuation is made and
    the output is returned as-is without raising AttributeError."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap: dict = {}

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")

        async def run(self, q, *, usage_limits=None, message_history=None):
            if self._name != "explore-retry":
                cap.setdefault("runs", []).append(q)
            r = type("R", (), {})()
            r.output = "answer without response"
            # no .response at all
            return r

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    _patch_explore_model(monkeypatch, {})

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "answer without response"
    assert len(cap["runs"]) == 1
