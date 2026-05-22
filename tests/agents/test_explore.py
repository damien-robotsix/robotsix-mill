"""The read-only exploration sub-agent."""

from robotsix_mill.agents import explore
from robotsix_mill.agents.explore import make_explore_tool
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    return Settings(**env)


def test_no_key_degrades_not_raises(tmp_path):
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = explore.run_explore(
        settings=s, repo_dir=tmp_path, question="where is X?"
    )
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_tool_delegates_to_seam(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    def fake(*, settings, repo_dir, question):
        seen["q"] = question
        seen["dir"] = repo_dir
        return f"FOUND: {question}"

    monkeypatch.setattr(explore, "run_explore", fake)
    tool = make_explore_tool(s, tmp_path)
    assert tool("where is the worker?") == "FOUND: where is the worker?"
    assert seen["q"] == "where is the worker?" and seen["dir"] == tmp_path


def test_explore_subagent_is_read_only_and_uses_explore_model(
    tmp_path, monkeypatch
):
    """The sub-agent gets ONLY read_file/list_dir (never write_file or
    run_command) and runs on its own explore_model, bounded."""
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
    assert cap["tools"] == ["list_dir", "read_file"]  # NO write/run
    assert cap["limit"] == 7
    assert cap["name"] == "explore"
