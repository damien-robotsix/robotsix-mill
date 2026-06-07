"""The read-only exploration sub-agent."""

import asyncio

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
    """parallel_explore runs one scout per question and returns every
    answer labeled by question."""
    s = _settings(tmp_path)

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        return f"ANS:{question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    out = asyncio.run(tool(["q1", "q2", "q3"]))
    assert "[1] q1" in out and "ANS:q1" in out
    assert "[2] q2" in out and "[3] q3" in out


def test_parallel_explore_bounds_concurrency(tmp_path, monkeypatch):
    """No more than ``parallel_explore_max`` scouts run at once."""
    s = _settings(tmp_path, parallel_explore_max=2)
    state = {"cur": 0, "max": 0}

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return question

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    asyncio.run(tool([f"q{i}" for i in range(6)]))
    assert state["max"] <= 2


def test_parallel_explore_isolates_per_slot_failures(tmp_path, monkeypatch):
    """One failing scout yields an error string for its slot; the rest
    still return."""
    s = _settings(tmp_path)

    async def fake(*, settings, repo_dir, question, extra_roots=None):
        if question == "boom":
            raise RuntimeError("nope")
        return f"ok:{question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    out = asyncio.run(tool(["a", "boom", "c"]))
    assert "ok:a" in out and "ok:c" in out
    assert "explore failed" in out and "nope" in out


def test_parallel_explore_empty_questions(tmp_path):
    s = _settings(tmp_path)
    tool = explore.make_parallel_explore_tool(s, tmp_path)
    assert "no questions" in asyncio.run(tool([]))


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


def test_explore_subagent_is_read_only_and_uses_explore_model(tmp_path, monkeypatch):
    """The sub-agent gets ONLY read_file/list_dir/run_command (never
    write_file/edit_file/delete_file) and runs on its own explore_model,
    bounded."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        model="coordinator/big",
        explore_model="explore/cheap",
        explore_request_limit="7",
        explore_max_tokens="600",
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in kw.get("tools", []))
            cap["name"] = kw.get("name")
            cap["model_settings"] = kw.get("model_settings")

        async def run(self, q, *, usage_limits=None):
            cap["limit"] = usage_limits.request_limit
            return type("R", (), {"output": "answer"})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    out = asyncio.run(explore.run_explore(settings=s, repo_dir=tmp_path, question="q"))
    assert out == "answer"
    assert cap["model"] == "explore/cheap"  # its own model, not coordinator
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
    s = _settings(tmp_path, OPENROUTER_API_KEY="k", explore_model="explore/cheap")
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, q, *, usage_limits=None):
            cap["prompt"] = q
            return type("R", (), {"output": "answer"})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

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
    s = _settings(tmp_path, OPENROUTER_API_KEY="k", explore_model="explore/cheap")
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, q, *, usage_limits=None):
            cap["prompt"] = q
            return type("R", (), {"output": "answer"})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    out = asyncio.run(
        explore.run_explore(settings=s, repo_dir=tmp_path, question="where is X?")
    )
    assert out == "answer"
    assert cap["prompt"] == "where is X?"


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
        explore_model="explore/cheap",
        explore_request_limit="20",
    )

    primary_agent_calls = []
    retry_agent_calls = []

    class FakeModel:
        def __init__(self, name, **kw):
            pass

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
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        pydantic_ai.exceptions, "UsageLimitExceeded", _FakeUsageLimitExceeded
    )
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

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
        explore_model="explore/cheap",
        explore_request_limit="20",
    )

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")

        async def run(self, q, *, usage_limits=None):
            raise _FakeUsageLimitExceeded("budget cap")

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        pydantic_ai.exceptions, "UsageLimitExceeded", _FakeUsageLimitExceeded
    )
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

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
