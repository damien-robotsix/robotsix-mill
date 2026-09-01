"""The read-only exploration sub-agent."""

import asyncio
import contextlib

import pytest
from pydantic import ValidationError

from robotsix_mill.agents import explore
from robotsix_mill.agents.explore import make_explore_tool
from robotsix_mill.config import Secrets, Settings, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    # The scout defaults to level 1 on the Claude default slot; the tests
    # below that patch ``pydantic_ai.Agent`` / ``build_openrouter_model``
    # exercise the OpenRouter path, so when the test picks no level itself,
    # arm llmio's failover window so level resolution lands on the
    # OpenRouter slot (the root autouse fixture resets the tracker).
    if "explore_model_level" not in env:
        env["explore_model_level"] = 1
        from robotsix_llmio.core.failover import get_failover_tracker
        from robotsix_llmio.exceptions import ProviderExhaustedError

        get_failover_tracker().record_failure(
            "default", ProviderExhaustedError("test: exercise the OpenRouter path")
        )
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
    assert "unavailable" in out
    assert "OPENROUTER_API_KEY" in out


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
    assert "q1" in out
    assert "q2" in out
    assert "q3" in out


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
    assert "cat" in sp
    assert "head" in sp
    assert "tail" in sp
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
    assert seen["q"] == "where is the worker?"
    assert seen["dir"] == tmp_path
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
    from robotsix_llmio.core.factory import default_tier_config

    from robotsix_mill.agents import base as bmod

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


def test_explore_subagent_is_read_only_and_uses_flash_model(
    tmp_path, monkeypatch, level1_model
):
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

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
    assert out == "answer"
    # level-1 (flash) DeepSeek model — resolved from llmio's tier defaults.
    assert cap["model"] == level1_model
    assert cap["tools"] == [
        "list_dir",
        "parallel_commands",
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
                    {
                        "name": self._name,
                        "tools": self._tools,
                        "system_prompt": self._system_prompt,
                    }
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

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
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
    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
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

    import pytest
    from pydantic import ValidationError

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

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
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
    assert "q1" in out
    assert "q2" in out
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

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
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

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
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

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
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

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
    assert out == "answer without response"
    assert len(cap["runs"]) == 1


# ===================================================================
# _extract_explored_paths
# ===================================================================


class TestExtractExploredPaths:
    """Tests for the path-extraction heuristic used by the explore
    tool wrapper to populate ``explore_served_files``."""

    def test_extracts_repo_relative_paths(self, tmp_path):
        """Paths like ``src/foo/bar.py`` that exist on disk are extracted."""
        (tmp_path / "src" / "foo").mkdir(parents=True)
        (tmp_path / "src" / "foo" / "bar.py").write_text("x = 1\n")
        result = explore._extract_explored_paths(
            "The relevant file is src/foo/bar.py at line 1.", tmp_path
        )
        assert len(result) == 1
        assert str((tmp_path / "src" / "foo" / "bar.py").resolve()) in result

    def test_ignores_nonexistent_paths(self, tmp_path):
        """Paths that don't exist on disk are silently skipped."""
        result = explore._extract_explored_paths(
            "Check src/missing/file.py for details.", tmp_path
        )
        assert len(result) == 0

    def test_extracts_multiple_paths(self, tmp_path):
        """Multiple paths in one response are all extracted."""
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "a.dart").write_text("a\n")
        (tmp_path / "lib" / "b.dart").write_text("b\n")
        result = explore._extract_explored_paths(
            "See lib/a.dart and lib/b.dart for the implementation.", tmp_path
        )
        assert len(result) == 2

    def test_empty_result(self, tmp_path):
        """Empty string returns empty set."""
        assert explore._extract_explored_paths("", tmp_path) == set()

    def test_no_paths_in_text(self, tmp_path):
        """Text with no path-like tokens returns empty set."""
        assert (
            explore._extract_explored_paths("No relevant files found.", tmp_path)
            == set()
        )

    def test_backtick_delimited_paths(self, tmp_path):
        """Paths wrapped in backticks are extracted."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod.py").write_text("pass\n")
        result = explore._extract_explored_paths(
            "The function lives in `pkg/mod.py`.", tmp_path
        )
        assert len(result) == 1

    def test_paths_with_dashes_and_underscores(self, tmp_path):
        """Paths containing dashes and underscores are matched."""
        (tmp_path / "my_dir").mkdir()
        (tmp_path / "my_dir" / "my_file-v2.py").write_text("pass\n")
        result = explore._extract_explored_paths(
            "Found in my_dir/my_file-v2.py.", tmp_path
        )
        assert len(result) == 1

    def test_path_at_end_of_string(self, tmp_path):
        """Paths at the very end of the response string are matched."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass\n")
        result = explore._extract_explored_paths(
            "The entry point is src/main.py", tmp_path
        )
        assert len(result) == 1
        assert str((tmp_path / "src" / "main.py").resolve()) in result


# ===================================================================
# explore_served_files integration
# ===================================================================


class TestExploreServedFiles:
    """Integration tests for the explore→read_file dedup bridge."""

    def test_explore_populates_served_files(self, tmp_path, monkeypatch):
        """After explore returns, paths from its result appear in
        ``explore_served_files``."""
        s = _settings(tmp_path, OPENROUTER_API_KEY="test-key")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('hi')\n")

        async def fake_run_explore(*, settings, repo_dir, question, **kw):
            return "The relevant file is src/app.py at line 1."

        monkeypatch.setattr(explore, "run_explore", fake_run_explore)

        served: set[str] = set()
        tool = explore.make_explore_tool(s, tmp_path, explore_served_files=served)
        asyncio.run(tool("find the app"))

        expected = str((tmp_path / "src" / "app.py").resolve())
        assert expected in served

    def test_read_file_refuses_explore_served_full_read(self, tmp_path, settings):
        """read_file returns a short marker for a file already served
        by explore when the request is a full/default read."""
        from robotsix_mill.agents.fs_tools import build_fs_tools

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n")

        served: set[str] = set()
        resolved = str((tmp_path / "src" / "app.py").resolve())
        served.add(resolved)

        tools = build_fs_tools(tmp_path, settings, explore_served_files=served)
        read_file = next(t for t in tools if t.__name__ == "read_file")

        result = read_file(path="src/app.py")
        assert "already in context" in result
        assert "explore sub-agent" in result

    def test_read_file_allows_explore_served_specific_range(self, tmp_path, settings):
        """read_file with explicit offset/limit still works even when
        the file is in explore_served_files — the explore snippet may
        not cover the requested region."""
        from robotsix_mill.agents.fs_tools import build_fs_tools

        content = "\n".join(f"line {i}" for i in range(1, 101))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "big.py").write_text(content + "\n")

        served: set[str] = set()
        resolved = str((tmp_path / "src" / "big.py").resolve())
        served.add(resolved)

        tools = build_fs_tools(tmp_path, settings, explore_served_files=served)
        read_file = next(t for t in tools if t.__name__ == "read_file")

        # Explicit offset/limit should still work.
        result = read_file(path="src/big.py", offset=50, limit=10)
        assert "line 50" in result
        assert "already in context" not in result

    def test_write_invalidates_explore_served(self, tmp_path, settings):
        """After write_file, the path is removed from
        explore_served_files so a subsequent read_file succeeds."""
        from robotsix_mill.agents.fs_tools import build_fs_tools

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n")

        served: set[str] = set()
        resolved = str((tmp_path / "src" / "app.py").resolve())
        served.add(resolved)

        tools = build_fs_tools(tmp_path, settings, explore_served_files=served)
        tool_map = {t.__name__: t for t in tools}

        # Write new content.
        tool_map["write_file"](path="src/app.py", content="x = 2\n")
        assert resolved not in served

        # Subsequent read_file should succeed (not refused).
        result = tool_map["read_file"](path="src/app.py")
        assert "x = 2" in result
        assert "already in context" not in result

    def test_edit_invalidates_explore_served(self, tmp_path, settings):
        """After edit_file, the path is removed from
        explore_served_files."""
        from robotsix_mill.agents.fs_tools import build_fs_tools

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n")

        served: set[str] = set()
        resolved = str((tmp_path / "src" / "app.py").resolve())
        served.add(resolved)

        tools = build_fs_tools(tmp_path, settings, explore_served_files=served)
        tool_map = {t.__name__: t for t in tools}

        tool_map["edit_file"](path="src/app.py", old_string="x = 1", new_string="x = 2")
        assert resolved not in served

    def test_delete_invalidates_explore_served(self, tmp_path, settings):
        """After delete_file, the path is removed from
        explore_served_files."""
        from robotsix_mill.agents.fs_tools import build_fs_tools

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n")

        served: set[str] = set()
        resolved = str((tmp_path / "src" / "app.py").resolve())
        served.add(resolved)

        tools = build_fs_tools(tmp_path, settings, explore_served_files=served)
        tool_map = {t.__name__: t for t in tools}

        tool_map["delete_file"](path="src/app.py")
        assert resolved not in served

    def test_none_explore_served_files_no_effect(self, tmp_path, settings):
        """When explore_served_files is None (default), read_file
        behaves normally — no refusal."""
        from robotsix_mill.agents.fs_tools import build_fs_tools

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n")

        tools = build_fs_tools(tmp_path, settings)
        read_file = next(t for t in tools if t.__name__ == "read_file")

        result = read_file(path="src/app.py")
        assert "x = 1" in result
        assert "already in context" not in result

    def test_parallel_explore_populates_served_files(self, tmp_path, monkeypatch):
        """After parallel_explore returns, paths from the combined
        result appear in ``explore_served_files``."""
        s = _settings(tmp_path, OPENROUTER_API_KEY="test-key")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('hi')\n")
        (tmp_path / "src" / "utils.py").write_text("def helper(): pass\n")

        async def fake_run_explore(*, settings, repo_dir, question, **kw):
            # Simulate a batched response mentioning both files
            return "src/app.py has the main entry. src/utils.py has helpers."

        monkeypatch.setattr(explore, "run_explore", fake_run_explore)

        served: set[str] = set()
        tool = explore.make_parallel_explore_tool(
            s, tmp_path, explore_served_files=served
        )
        asyncio.run(tool(["find the app", "find the utils"]))

        expected_app = str((tmp_path / "src" / "app.py").resolve())
        expected_utils = str((tmp_path / "src" / "utils.py").resolve())
        assert expected_app in served
        assert expected_utils in served


# ---------------------------------------------------------------------------
# explore_model_level — the scout runs on a configurable tier (default haiku)
# ---------------------------------------------------------------------------


def test_explore_model_level_defaults_to_haiku_tier(tmp_path):
    """Default level is 1 — haiku on the Claude subscription, not paid flash."""
    s = Settings(data_dir=str(tmp_path))
    assert s.explore_model_level == 1
    for bad in (0, 4):
        with pytest.raises(ValidationError):
            Settings(data_dir=str(tmp_path), explore_model_level=bad)


def test_explore_openrouter_tier_uses_configured_level(tmp_path, monkeypatch):
    """A non-Claude ``explore_model_level`` reaches ``build_openrouter_model``
    (the scout is no longer hard-wired to level 1)."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k", explore_model_level="3")
    # Explicit level: arm the failover window so 3 resolves the OpenRouter slot.
    from robotsix_llmio.core.failover import get_failover_tracker
    from robotsix_llmio.exceptions import ProviderExhaustedError

    get_failover_tracker().record_failure(
        "default", ProviderExhaustedError("test: exercise the OpenRouter path")
    )
    cap = {}

    from robotsix_mill.agents import base as bmod

    def fake_build_openrouter_model(level=1, *, online=False):
        cap["level"] = level
        return object(), object()

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, q, *, usage_limits=None):
            cap["limit"] = usage_limits.request_limit
            return type("R", (), {"output": "answer"})()

    import pydantic_ai

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(bmod, "build_openrouter_model", fake_build_openrouter_model)
    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
    assert out == "answer"
    assert cap["level"] == 3
    assert cap["limit"] == s.explore_request_limit


class _FakeClaudeProvider:
    """Stands in for llmio's ``ClaudeSDKProvider`` at the provider seam."""

    def __init__(self, cap, output="haiku answer", error=None):
        self._cap = cap
        self._output = output
        self._error = error

    def build_agent(self, **kw):
        self._cap["build"] = kw
        cap, output, error = self._cap, self._output, self._error

        class Handle:
            async def run(self, prompt, **run_kw):
                cap["prompt"] = prompt
                cap["run_kwargs"] = run_kw
                if error is not None:
                    raise error
                return type("R", (), {"output": output})()

            def close(self):
                pass

        return Handle()


def _patch_claude_provider(monkeypatch, provider):
    """Patch llmio's ``get_provider_for_level`` (the seam ``build_subagent``
    resolves at call time) so no ``claude`` CLI is ever spawned."""
    import robotsix_llmio

    calls = {}

    def fake_get_provider_for_level(level, **kw):
        calls["level"] = level
        calls["kwargs"] = kw
        return provider

    monkeypatch.setattr(
        robotsix_llmio, "get_provider_for_level", fake_get_provider_for_level
    )
    return calls


def test_explore_claude_tier_builds_via_provider_with_read_only_tools(
    tmp_path, monkeypatch
):
    """On a Claude tier the scout is built through the llmio provider (a raw
    pydantic-ai Agent cannot carry tools there): the configured level and the
    read-only tool subset reach ``provider.build_agent``, the SDK's built-in
    tools are denied, and no OpenRouter key or ``usage_limits`` is required."""
    (tmp_path / "a.txt").write_text("hi")
    # No OPENROUTER_API_KEY: the Claude tier is keyless.
    s = _settings(tmp_path, explore_model_level="2", explore_max_tokens="600")
    cap = {}
    calls = _patch_claude_provider(monkeypatch, _FakeClaudeProvider(cap))

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
    assert out == "haiku answer"
    assert calls["level"] == 2
    assert calls["kwargs"] == {"max_tokens": 600}
    build = cap["build"]
    assert build["level"] == 2
    assert build["name"] == "explore"
    assert build["output_type"] is str
    assert build["builtin_tools"] is False
    assert build["workspace_root"] == tmp_path
    assert sorted(t.__name__ for t in build["tools"]) == [
        "list_dir",
        "parallel_commands",
        "read_file",
        "run_command",
    ]  # NO write/edit/delete
    # The SDK tool loop cannot honour usage_limits — none is forwarded.
    assert cap["run_kwargs"] == {}


def test_explore_claude_usage_exhausted_surfaces_without_paid_fallback(
    tmp_path, monkeypatch
):
    """Subscription quota exhaustion is non-transient: the scout fails once,
    does not fall back to a paid tier, and the failure surfaces to the caller
    like any other explore failure (the worker parks at stage level)."""
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKUsageExhaustedError

    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, explore_model_level="2")
    cap = {}
    err = ClaudeSDKUsageExhaustedError("You've hit your usage limit")
    _patch_claude_provider(monkeypatch, _FakeClaudeProvider(cap, error=err))
    from robotsix_mill.agents import base as bmod

    def no_openrouter(*a, **k):
        raise AssertionError("must not fall back to a paid OpenRouter tier")

    monkeypatch.setattr(bmod, "build_openrouter_model", no_openrouter)
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(explore.asyncio, "sleep", fake_sleep)

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
    assert out.startswith("explore failed")
    assert "usage limit" in out
    assert sleeps == []  # non-transient: no retry ladder


def test_explore_claude_usage_exhausted_falls_back_with_paid_flag(
    tmp_path, monkeypatch
):
    """With ``provider_failover_enabled=true``, an exhausted Claude explore
    call arms llmio's failover window and is rebuilt at the SAME level on
    the OpenRouter fallback slot — the caller gets a real answer."""
    from robotsix_llmio.claude_sdk._errors import ClaudeSDKUsageExhaustedError

    (tmp_path / "a.txt").write_text("hi")
    s = _settings(
        tmp_path,
        explore_model_level="2",
        provider_failover_enabled=True,
        OPENROUTER_API_KEY="fallback-key",
    )

    claude_err = ClaudeSDKUsageExhaustedError("You've hit your session limit")
    claude_cap = {}
    _patch_claude_provider(
        monkeypatch, _FakeClaudeProvider(claude_cap, error=claude_err)
    )

    # Fallback-slot agent (same level, OpenRouter provider) that succeeds.
    fallback_cap: dict = {}

    class FallbackAgent:
        def __init__(self, **kw):
            fallback_cap["tools"] = sorted(t.__name__ for t in kw.get("tools", []))
            fallback_cap["name"] = kw.get("name")

        async def run(self, q, *, usage_limits=None):
            fallback_cap["prompt"] = q
            fallback_cap["limit"] = usage_limits.request_limit if usage_limits else None
            return type("R", (), {"output": "fallback answer"})()

    import pydantic_ai

    # Patch pydantic_ai.Agent so the OpenRouter fallback path uses our fake.
    monkeypatch.setattr(pydantic_ai, "Agent", FallbackAgent)

    from robotsix_mill.agents import base as bmod

    def fake_build_openrouter_model(level=1, *, online=False):
        fallback_cap["level"] = level
        return object(), object()

    monkeypatch.setattr(bmod, "build_openrouter_model", fake_build_openrouter_model)

    out = asyncio.run(
        explore.run_explore(
            settings=s, repo_dir=tmp_path, question="a test question long enough"
        )
    )
    assert out == "fallback answer"
    # The fallback stayed at the SAME level — only the provider changed.
    assert fallback_cap["level"] == 2
    assert fallback_cap["name"] == "explore"
    # The same question was forwarded (not simplified).
    assert "a test question long enough" in fallback_cap["prompt"]
    # Read-only tools were passed to the fallback agent.
    assert fallback_cap["tools"] == [
        "list_dir",
        "parallel_commands",
        "read_file",
        "run_command",
    ]
    # UsageLimits were set for the non-Claude fallback.
    assert fallback_cap["limit"] == s.explore_request_limit
