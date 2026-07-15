"""Tests for the answering agent — langfuse tools and run_answer_agent."""

from robotsix_mill.agents.answering import _build_langfuse_tools, run_answer_agent
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    # OPENROUTER_API_KEY is now a Secrets-only field; pop before Settings()
    env.pop("OPENROUTER_API_KEY", None)
    return Settings(**env)


# ── _build_langfuse_tools tests ──────────────────────────────────────


def test_fetch_session_cost_formats_dollar_string(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: 1.2345,
    )
    tools = _build_langfuse_tools(s)
    fetch = tools[0]
    assert fetch.__name__ == "langfuse_session_cost"
    assert fetch("s1") == "$1.2345"


def test_fetch_session_cost_handles_zero(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: 0.0,
    )
    tools = _build_langfuse_tools(s)
    fetch = tools[0]
    assert fetch("s1") == "$0.0000"


def test_fetch_session_summary_returns_summary(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid, repo_config=None: "## Session summary",
    )
    tools = _build_langfuse_tools(s)
    summary_fn = tools[1]
    assert summary_fn("s1") == "## Session summary"


def test_fetch_session_summary_handles_none(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid, repo_config=None: None,
    )
    tools = _build_langfuse_tools(s)
    summary_fn = tools[1]
    result = summary_fn("s1")
    assert "No Langfuse data found" in result
    assert "s1" in result


def test_list_traces_returns_formatted_lines(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params, repo_config=None: {
            "data": [
                {
                    "id": "trace-1",
                    "name": "my-trace",
                    "timestamp": "2025-01-01T00:00:00Z",
                    "totalCost": 0.5,
                },
                {
                    "id": "trace-2",
                    "name": "other",
                    "timestamp": "2025-01-02T00:00:00Z",
                    "totalCost": 1.25,
                },
            ]
        },
    )
    tools = _build_langfuse_tools(s)
    list_fn = tools[2]
    result = list_fn("s1")
    assert "trace-1  my-trace  2025-01-01T00:00:00Z  $0.5000" in result
    assert "trace-2  other  2025-01-02T00:00:00Z  $1.2500" in result


def test_list_traces_handles_none_data(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params, repo_config=None: None,
    )
    tools = _build_langfuse_tools(s)
    list_fn = tools[2]
    assert list_fn("s1") == "Langfuse unavailable or tracing not configured"


def test_list_traces_handles_empty_data(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params, repo_config=None: {"data": []},
    )
    tools = _build_langfuse_tools(s)
    list_fn = tools[2]
    result = list_fn("s1")
    assert "No traces found" in result
    assert "s1" in result


def test_fetch_trace_detail_returns_summary(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: {
            "name": "my-trace",
            "id": "trace-1",
            "timestamp": "2025-01-01T00:00:00Z",
            "totalCost": 0.75,
            "latency": 2.5,
            "observations": [
                {"level": "ERROR"},
                {"level": "DEFAULT"},
                {"level": "ERROR"},
            ],
        },
    )
    tools = _build_langfuse_tools(s)
    detail_fn = tools[3]
    result = detail_fn("t1")
    assert "trace: my-trace" in result
    assert "id: trace-1" in result
    assert "cost: $0.7500" in result
    assert "latency: 2.5s" in result
    assert "observations: 3" in result
    assert "DEFAULT=1" in result
    assert "ERROR=2" in result


def test_fetch_trace_detail_handles_none(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: None,
    )
    tools = _build_langfuse_tools(s)
    detail_fn = tools[3]
    result = detail_fn("t1")
    assert "No trace found" in result
    assert "t1" in result


def test_repo_config_forwarded_to_client(tmp_path, monkeypatch):
    """_build_langfuse_tools forwards repo_config to the client functions."""
    from robotsix_mill.config import RepoConfig

    rc = RepoConfig(
        repo_id="my-repo",
        board_id="b1",
        langfuse_project_name="proj",
        langfuse_public_key="pk-rc",
        langfuse_secret_key="sk-rc",
    )

    captured = {}

    def _spy_session_cost(settings, session_id, repo_config=None):
        captured["repo_config"] = repo_config
        return 1.0

    monkeypatch.setattr("robotsix_mill.langfuse.client.session_cost", _spy_session_cost)

    s = _settings(tmp_path)
    tools = _build_langfuse_tools(s, repo_config=rc)
    fetch = tools[0]
    result = fetch("s1")
    assert result == "$1.0000"
    assert captured["repo_config"] is rc


# ── run_answer_agent tests ───────────────────────────────────────────


def _install_mocks(monkeypatch):
    """Install shared mocks for load_agent_definition, run_agent, and
    _safe_close.  Returns (base_mod, retry_mod) for further patching."""
    from unittest.mock import MagicMock
    import robotsix_mill.agents.yaml_loader as yaml_loader_mod
    import robotsix_mill.agents.retry as retry_mod
    import robotsix_mill.agents.base as base_mod

    monkeypatch.setattr(
        yaml_loader_mod,
        "load_agent_definition",
        MagicMock(return_value=type("D", (), {"level": 2})()),
    )
    monkeypatch.setattr(base_mod, "_safe_close", lambda agent: None)
    return base_mod, retry_mod


def test_run_answer_agent_without_repo_dir(tmp_path, monkeypatch):
    """Without repo_dir: no explore/fs tools, but langfuse tools still present."""
    bmod, rmod = _install_mocks(monkeypatch)

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap = {}

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        cap["tools"] = sorted(t.__name__ for t in tools)
        cap["level"] = level

        class FakeAgent:
            def run_sync(self, prompt):
                class R:
                    output = "the answer"

                return R()

        return FakeAgent()

    def fake_retry(agent, make_run, *, what="model call"):
        return make_run(agent)

    monkeypatch.setattr(bmod, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(rmod, "run_agent", fake_retry)
    # Stub langfuse tools to avoid real imports
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid, repo_config=None: "summary",
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params, repo_config=None: {"data": []},
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: None,
    )

    result = run_answer_agent(settings=s, title="T", question="Q")
    assert result == "the answer"
    # No explore or fs tools
    assert "explore" not in cap["tools"]
    assert "read_file" not in cap["tools"]
    # Langfuse tools are present
    assert "langfuse_session_cost" in cap["tools"]
    assert "langfuse_session_summary" in cap["tools"]
    assert "langfuse_list_traces" in cap["tools"]
    assert "langfuse_trace_detail" in cap["tools"]


def test_run_answer_agent_with_repo_dir(tmp_path, monkeypatch):
    """With repo_dir: explore + fs read-only tools + langfuse tools."""
    bmod, rmod = _install_mocks(monkeypatch)

    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap = {}

    def fake_build_agent(settings, definition, tools, level, repo_dir=None, **kw):
        cap["tools"] = sorted(t.__name__ for t in tools)
        cap["level"] = level

        class FakeAgent:
            def run_sync(self, prompt):
                class R:
                    output = "the answer"

                return R()

        return FakeAgent()

    def fake_retry(agent, make_run, *, what="model call"):
        return make_run(agent)

    monkeypatch.setattr(bmod, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(rmod, "run_agent", fake_retry)
    # Stub langfuse tools
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid, repo_config=None: "summary",
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params, repo_config=None: {"data": []},
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: None,
    )

    result = run_answer_agent(settings=s, title="T", question="Q", repo_dir=tmp_path)
    assert result == "the answer"
    # Explore and fs tools present
    assert "explore" in cap["tools"]
    assert "read_file" in cap["tools"]
    assert "list_dir" in cap["tools"]
    assert "run_command" in cap["tools"]
    # Write tools must NOT be present
    for banned in ("edit_file", "write_file", "delete_file"):
        assert banned not in cap["tools"], f"{banned} should not be in answer tools"
    # Langfuse tools present
    assert "langfuse_session_cost" in cap["tools"]
    assert "langfuse_session_summary" in cap["tools"]
    assert "langfuse_list_traces" in cap["tools"]
    assert "langfuse_trace_detail" in cap["tools"]


def test_run_answer_agent_runtime_error_on_missing_api_key(tmp_path, monkeypatch):
    """Missing OpenRouter API key propagates as RuntimeError."""
    import robotsix_mill.agents.yaml_loader as yaml_loader_mod

    s = _settings(tmp_path, OPENROUTER_API_KEY="")

    def fake_load_and_run(**kw):
        raise RuntimeError("OPENROUTER_API_KEY is required")

    monkeypatch.setattr(yaml_loader_mod, "load_and_run_agent", fake_load_and_run)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid, repo_config=None: "summary",
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params, repo_config=None: {"data": []},
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: None,
    )

    import pytest

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        run_answer_agent(settings=s, title="T", question="Q")
