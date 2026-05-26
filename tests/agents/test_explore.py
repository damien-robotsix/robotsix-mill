"""The read-only exploration sub-agent."""

from robotsix_mill.agents import explore
from robotsix_mill.agents.explore import make_explore_tool
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    # Mirror openrouter_api_key into Secrets so get_secrets() works
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg
        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    return Settings(**env)


def test_no_key_degrades_not_raises(tmp_path):
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = explore.run_explore(
        settings=s, repo_dir=tmp_path, question="where is X?"
    )
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_missing_repo_degrades_not_raises(tmp_path):
    """When repo_dir does not exist, run_explore returns an
    'explore unavailable' message without importing pydantic_ai or
    making any HTTP call."""
    missing = tmp_path / "nonexistent"
    s = _settings(tmp_path, OPENROUTER_API_KEY="valid-key")
    out = explore.run_explore(
        settings=s, repo_dir=missing, question="where is X?"
    )
    assert "explore unavailable" in out
    assert "workspace repo directory does not exist" in out
    assert "not been cloned yet" in out


def test_tool_delegates_to_seam(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    def fake(*, settings, repo_dir, question, extra_roots=None):
        seen["q"] = question
        seen["dir"] = repo_dir
        seen["extra_roots"] = extra_roots
        return f"FOUND: {question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = make_explore_tool(s, tmp_path)
    assert tool("where is the worker?") == "FOUND: where is the worker?"
    assert seen["q"] == "where is the worker?" and seen["dir"] == tmp_path
    assert seen["extra_roots"] is None


def test_explore_subagent_is_read_only_and_uses_explore_model(
    tmp_path, monkeypatch
):
    """The sub-agent gets ONLY read_file/list_dir/run_command (never
    write_file/edit_file/delete_file) and runs on its own explore_model,
    bounded."""
    (tmp_path / "a.txt").write_text("hi")
    s = _settings(
        tmp_path, OPENROUTER_API_KEY="k",
        MILL_MODEL="coordinator/big", MILL_EXPLORE_MODEL="explore/cheap",
        MILL_EXPLORE_REQUEST_LIMIT="7",
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in kw.get("tools", []))
            cap["name"] = kw.get("name")

        def run_sync(self, q, *, usage_limits=None):
            cap["limit"] = usage_limits.request_limit
            return type("R", (), {"output": "answer"})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    out = explore.run_explore(settings=s, repo_dir=tmp_path, question="q")
    assert out == "answer"
    assert cap["model"] == "explore/cheap"  # its own model, not coordinator
    assert cap["tools"] == ["list_dir", "read_file", "run_command"]  # NO write/edit/delete
    assert cap["limit"] == 7
    assert cap["name"] == "explore"


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
        tmp_path, OPENROUTER_API_KEY="k",
        MILL_EXPLORE_MODEL="explore/cheap",
        MILL_EXPLORE_REQUEST_LIMIT="20",
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
                retry_agent_calls.append(dict(
                    name=self._name,
                    tools=self._tools,
                    system_prompt=self._system_prompt,
                ))

        def run_sync(self, q, *, usage_limits=None):
            if self._name == "explore":
                primary_agent_calls.append(1)
                raise _FakeUsageLimitExceeded("budget cap")
            # explore-retry succeeds
            return type("R", (), {"output": "retry-answer"})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(pydantic_ai.exceptions, "UsageLimitExceeded",
                        _FakeUsageLimitExceeded)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    out = explore.run_explore(settings=s, repo_dir=tmp_path, question="q")
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
        tmp_path, OPENROUTER_API_KEY="k",
        MILL_EXPLORE_MODEL="explore/cheap",
        MILL_EXPLORE_REQUEST_LIMIT="20",
    )

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            self._name = kw.get("name", "")

        def run_sync(self, q, *, usage_limits=None):
            raise _FakeUsageLimitExceeded("budget cap")

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(pydantic_ai.exceptions, "UsageLimitExceeded",
                        _FakeUsageLimitExceeded)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    # Reset sentinel before test
    explore.reset_explore_budget_exhausted()
    out = explore.run_explore(settings=s, repo_dir=tmp_path, question="q")
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
