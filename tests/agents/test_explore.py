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
