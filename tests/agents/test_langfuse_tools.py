"""Tests for the shared langfuse_tools module."""

import asyncio

from robotsix_mill.agents.langfuse_tools import (
    _build_langfuse_tools,
    make_langfuse_inspect_tool,
    make_cost_inspect_tool,
)
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


def test_langfuse_session_cost_formats_dollar_string(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost", lambda settings, sid: 1.2345
    )
    tools = _build_langfuse_tools(s)
    fetch = tools[0]
    assert fetch.__name__ == "langfuse_session_cost"
    assert fetch("s1") == "$1.2345"


def test_langfuse_session_cost_handles_zero(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost", lambda settings, sid: 0.0
    )
    tools = _build_langfuse_tools(s)
    fetch = tools[0]
    assert fetch("s1") == "$0.0000"


def test_langfuse_session_summary_returns_summary(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid: "## Session summary",
    )
    tools = _build_langfuse_tools(s)
    summary_fn = tools[1]
    assert summary_fn.__name__ == "langfuse_session_summary"
    assert summary_fn("s1") == "## Session summary"


def test_langfuse_session_summary_handles_none(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid: None,
    )
    tools = _build_langfuse_tools(s)
    summary_fn = tools[1]
    result = summary_fn("s1")
    assert "No Langfuse data found" in result
    assert "s1" in result


def test_langfuse_list_traces_returns_formatted_lines(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params: {
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
    assert list_fn.__name__ == "langfuse_list_traces"
    result = list_fn("s1")
    assert "trace-1  my-trace  2025-01-01T00:00:00Z  $0.5000" in result
    assert "trace-2  other  2025-01-02T00:00:00Z  $1.2500" in result


def test_langfuse_list_traces_handles_none_data(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params: None,
    )
    tools = _build_langfuse_tools(s)
    list_fn = tools[2]
    assert list_fn("s1") == "Langfuse unavailable or tracing not configured"


def test_langfuse_list_traces_handles_empty_data(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params: {"data": []},
    )
    tools = _build_langfuse_tools(s)
    list_fn = tools[2]
    result = list_fn("s1")
    assert "No traces found" in result
    assert "s1" in result


def test_langfuse_trace_detail_returns_summary(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {
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
    assert detail_fn.__name__ == "langfuse_trace_detail"
    result = detail_fn("t1")
    assert "trace: my-trace" in result
    assert "id: trace-1" in result
    assert "cost: $0.7500" in result
    assert "latency: 2.5s" in result
    assert "observations: 3" in result
    assert "DEFAULT=1" in result
    assert "ERROR=2" in result


def test_langfuse_trace_detail_handles_none(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: None,
    )
    tools = _build_langfuse_tools(s)
    detail_fn = tools[3]
    result = detail_fn("t1")
    assert "No trace found" in result
    assert "t1" in result


# ── _build_langfuse_tools returns four callables ─────────────────────


def test_build_langfuse_tools_returns_four_callables(tmp_path, monkeypatch):
    """_build_langfuse_tools returns exactly four callables."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost", lambda settings, sid: 0.0
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid: "summary",
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params: {"data": []},
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: None,
    )
    tools = _build_langfuse_tools(s)
    assert len(tools) == 4
    for t in tools:
        assert callable(t), f"{t} is not callable"
    names = [t.__name__ for t in tools]
    assert names == [
        "langfuse_session_cost",
        "langfuse_session_summary",
        "langfuse_list_traces",
        "langfuse_trace_detail",
    ]


# ── make_langfuse_inspect_tool tests ─────────────────────────────────


def test_make_langfuse_inspect_tool_returns_callable(tmp_path):
    """make_langfuse_inspect_tool returns a callable closure."""
    s = _settings(tmp_path)
    tool = make_langfuse_inspect_tool(s)
    assert callable(tool)
    assert tool.__name__ == "langfuse_inspect_trace"


def test_langfuse_inspect_trace_degradation_trace_unavailable(tmp_path, monkeypatch):
    """When fetch_trace_detail returns None, the tool returns a
    degradation message."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: None,
    )
    tool = make_langfuse_inspect_tool(s)
    output = asyncio.run(tool("missing-trace"))
    assert "trace missing-trace unavailable" in output


def test_langfuse_inspect_trace_delegates_to_run_trace_inspector(tmp_path, monkeypatch):
    """The tool fetches the trace and delegates to run_trace_inspector,
    returning its formatted output."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {"id": tid, "name": "test", "observations": []},
    )

    from robotsix_mill.agents.trace_inspector import (
        TraceFinding,
        TraceInspectResult,
    )

    captured_kwargs = {}

    def fake_run_trace_inspector(**kwargs):
        captured_kwargs.update(kwargs)
        return TraceInspectResult(
            findings=[
                TraceFinding(
                    category="tool_error",
                    symptom="error A",
                    root_cause="",
                    proposed_solution="fix it",
                ),
            ]
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        fake_run_trace_inspector,
    )

    tool = make_langfuse_inspect_tool(s)
    output = asyncio.run(tool("trace-1"))

    assert "## trace trace-1 inspection" in output
    assert "### Tool Errors" in output
    assert "- error A" in output
    assert "_(fix: fix it)_" in output

    # Verify delegation to run_trace_inspector
    assert captured_kwargs["settings"] is s
    assert "trace_data" in captured_kwargs


def test_langfuse_inspect_trace_passes_repo_dir(tmp_path, monkeypatch):
    """When repo_dir is given to the factory, it is passed through to
    run_trace_inspector."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {"id": tid, "name": "test", "observations": []},
    )

    captured_kwargs = {}

    def fake_run_trace_inspector(**kwargs):
        captured_kwargs.update(kwargs)
        from robotsix_mill.agents.trace_inspector import TraceInspectResult

        return TraceInspectResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        fake_run_trace_inspector,
    )

    tool = make_langfuse_inspect_tool(s, repo_dir=repo)
    asyncio.run(tool("trace-1"))

    assert captured_kwargs["repo_dir"] == repo


def test_langfuse_inspect_trace_no_repo_dir(tmp_path, monkeypatch):
    """When repo_dir is None, it is passed as None to run_trace_inspector."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {"id": tid, "name": "test", "observations": []},
    )

    captured_kwargs = {}

    def fake_run_trace_inspector(**kwargs):
        captured_kwargs.update(kwargs)
        from robotsix_mill.agents.trace_inspector import TraceInspectResult

        return TraceInspectResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        fake_run_trace_inspector,
    )

    tool = make_langfuse_inspect_tool(s, repo_dir=None)
    asyncio.run(tool("trace-1"))

    assert captured_kwargs["repo_dir"] is None


def test_langfuse_inspect_trace_clean_no_issues(tmp_path, monkeypatch):
    """When no findings, a 'no issues' message is included."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {"id": tid, "name": "clean", "observations": []},
    )

    from robotsix_mill.agents.trace_inspector import TraceInspectResult

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        lambda **kwargs: TraceInspectResult(),
    )

    tool = make_langfuse_inspect_tool(s)
    output = asyncio.run(tool("clean-trace"))
    assert "(no issues found in this trace)" in output
    assert "### Tool Errors" not in output


def test_langfuse_inspect_trace_surfaces_inspector_error(tmp_path, monkeypatch):
    """When run_trace_inspector returns an error, it is surfaced."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {"id": tid, "name": "test", "observations": []},
    )

    from robotsix_mill.agents.trace_inspector import TraceInspectResult

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        lambda **kwargs: TraceInspectResult(error="context length exceeded"),
    )

    tool = make_langfuse_inspect_tool(s)
    output = asyncio.run(tool("trace-1"))
    assert "_inspector error: context length exceeded_" in output


def test_langfuse_inspect_trace_passes_request_limit_override(tmp_path, monkeypatch):
    """The tool passes request_limit_override from settings to
    run_trace_inspector."""
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        trace_review_tool_request_limit=12,
    )

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {"id": tid, "name": "test", "observations": []},
    )

    captured_kwargs = {}

    def fake_run_trace_inspector(**kwargs):
        captured_kwargs.update(kwargs)
        from robotsix_mill.agents.trace_inspector import TraceInspectResult

        return TraceInspectResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        fake_run_trace_inspector,
    )

    tool = make_langfuse_inspect_tool(s)
    asyncio.run(tool("trace-1"))

    assert captured_kwargs["request_limit_override"] == 12


def test_langfuse_inspect_trace_offloads_blocking_work_from_event_loop(
    tmp_path, monkeypatch
):
    """The tool runs ``run_trace_inspector`` (which calls ``run_sync`` →
    ``run_until_complete``) on a worker thread via ``asyncio.to_thread``
    so it never executes on the caller's live event loop."""
    import inspect

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: {"id": tid, "name": "test", "observations": []},
    )

    from robotsix_mill.agents.trace_inspector import (
        TraceFinding,
        TraceInspectResult,
    )

    captured_loop_free = {}

    def fake_run_trace_inspector(**kwargs):
        # Assert we are NOT on an active event loop — `asyncio.to_thread`
        # creates a bare worker thread with no loop running.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            captured_loop_free["no_loop"] = True
        else:
            captured_loop_free["no_loop"] = False
        return TraceInspectResult(
            findings=[
                TraceFinding(
                    category="optimization",
                    symptom="test finding",
                    root_cause="",
                    proposed_solution="test fix",
                ),
            ]
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        fake_run_trace_inspector,
    )

    tool = make_langfuse_inspect_tool(s)
    assert inspect.iscoroutinefunction(tool), "langfuse_inspect_trace must be async"

    async def driver():
        # We are on a running loop — like the Claude SDK tool callback.
        return await tool("trace-1")

    output = asyncio.run(driver())

    # The stub must have observed no running loop in its thread.
    assert captured_loop_free.get("no_loop") is True, (
        "run_trace_inspector ran on a thread WITH an active event loop — "
        "asyncio.to_thread did not isolate it"
    )

    # The tool must return rendered findings, not a raw event-loop error.
    assert "## trace trace-1 inspection" in output
    assert "### Optimizations" in output
    assert "This event loop is already running" not in output


# ── make_cost_inspect_tool tests ─────────────────────────────────────


def test_make_cost_inspect_tool_returns_callable(tmp_path):
    """make_cost_inspect_tool returns a callable closure."""
    s = _settings(tmp_path)
    tool = make_cost_inspect_tool(s)
    assert callable(tool)
    assert tool.__name__ == "inspect_cost"


def test_inspect_cost_no_traces_zero_total(tmp_path, monkeypatch):
    """When there are no traces and session cost is 0, a simple
    message is returned."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid: [],
    )
    tool = make_cost_inspect_tool(s)
    output = tool("s1")
    assert "session total: $0.0000" in output
    assert "trace count: 0" in output
    assert "no traces" in output


def test_inspect_cost_no_traces_nonzero_total(tmp_path, monkeypatch):
    """When session total is non-zero but no traces are returned,
    a discrepancy is flagged."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 5.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid: [],
    )
    tool = make_cost_inspect_tool(s)
    output = tool("s1")
    assert "session total: $5.0000" in output
    assert "DISCREPANCY" in output
    assert "provider attribution is unavailable" in output


def test_inspect_cost_traces_unavailable(tmp_path, monkeypatch):
    """When Langfuse is unreachable (traces returns None), a
    degradation message is returned."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid: None,
    )
    tool = make_cost_inspect_tool(s)
    output = tool("s1")
    assert "unavailable" in output
    assert "s1" in output


def test_inspect_cost_balanced(tmp_path, monkeypatch):
    """When per-trace costs sum to the session total, no discrepancy
    is flagged."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 2.5,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid: [
            {
                "name": "trace-a",
                "cost": 1.0,
                "at": "2025-01-01T00:00:00Z",
                "trace_id": "t1",
                "latency": 1.0,
                "model": "openai/gpt-4o",
            },
            {
                "name": "trace-b",
                "cost": 1.5,
                "at": "2025-01-01T00:01:00Z",
                "trace_id": "t2",
                "latency": 2.0,
                "model": "openrouter/anthropic/claude-sonnet",
            },
        ],
    )
    tool = make_cost_inspect_tool(s)
    output = tool("s1")
    assert "session total: $2.5000" in output
    assert "sum of per-trace costs: $2.5000" in output
    assert "trace-a" in output
    assert "trace-b" in output
    assert "openai/gpt-4o" in output
    assert "openrouter/anthropic/claude-sonnet" in output
    assert "DISCREPANCY" not in output


def test_inspect_cost_discrepancy_sum_vs_total(tmp_path, monkeypatch):
    """When per-trace sum ≠ session total, a discrepancy is flagged."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 10.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid: [
            {
                "name": "trace-a",
                "cost": 3.0,
                "at": "2025-01-01T00:00:00Z",
                "trace_id": "t1",
                "latency": 1.0,
                "model": "",
            },
        ],
    )
    tool = make_cost_inspect_tool(s)
    output = tool("s1")
    assert "session total: $10.0000" in output
    assert "sum of per-trace costs: $3.0000" in output
    assert "DISCREPANCY" in output
    assert "diff $+7.0000" in output


def test_inspect_cost_zero_cost_traces_flag(tmp_path, monkeypatch):
    """Traces with $0.00 cost are flagged when the session total is
    non-zero."""
    s = _settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 5.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid: [
            {
                "name": "openrouter-trace",
                "cost": 0.0,
                "at": "2025-01-01T00:00:00Z",
                "trace_id": "t1",
                "latency": 1.0,
                "model": "openrouter/openai/gpt-4o",
            },
        ],
    )
    tool = make_cost_inspect_tool(s)
    output = tool("s1")
    assert "session total: $5.0000" in output
    assert "sum of per-trace costs: $0.0000" in output
    assert "DISCREPANCY" in output
    assert "trace(s) with $0.00 cost" in output
    assert "openrouter-trace" in output


def test_inspect_cost_passes_repo_dir(tmp_path, monkeypatch):
    """When repo_dir is given to the factory, the tool still works
    (repo_dir is a gate, not forwarded to client calls currently)."""
    s = _settings(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid: [],
    )
    tool = make_cost_inspect_tool(s, repo_dir=repo)
    assert callable(tool)
    output = tool("s1")
    assert "session total: $0.0000" in output


# ── per-repo Langfuse credential resolution (repo_config) ────────────


def _repo_config():
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="robotsix-auto-mail",
        board_id="auto-mail",
        langfuse_project_name="auto-mail-project",
        langfuse_public_key="pk-repo",
        langfuse_secret_key="sk-repo",
        langfuse_base_url="https://repo.langfuse.example",
    )


def test_build_langfuse_tools_threads_repo_config(tmp_path, monkeypatch):
    """The four simple closures forward the per-repo repo_config to the
    client calls when one is supplied."""
    s = _settings(tmp_path)
    rc = _repo_config()
    captured = {}

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: (
            captured.update(cost=repo_config) or 0.0
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid, repo_config=None: (
            captured.update(summary=repo_config) or "ok"
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params, repo_config=None: (
            captured.update(list=repo_config) or {"data": []}
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: (
            captured.update(detail=repo_config) or None
        ),
    )

    tools = _build_langfuse_tools(s, repo_config=rc)
    cost_fn, summary_fn, list_fn, detail_fn = tools
    cost_fn("s1")
    summary_fn("s1")
    list_fn("s1")
    detail_fn("t1")

    assert captured["cost"] is rc
    assert captured["summary"] is rc
    assert captured["list"] is rc
    assert captured["detail"] is rc


def test_inspect_cost_resolves_repo_credentials(tmp_path, monkeypatch):
    """make_cost_inspect_tool forwards repo_config to session_cost /
    session_traces when supplied."""
    s = _settings(tmp_path)
    rc = _repo_config()
    captured = {}

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: (
            captured.update(cost=repo_config) or 0.0
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid, repo_config=None: (
            captured.update(traces=repo_config) or []
        ),
    )

    tool = make_cost_inspect_tool(s, repo_dir=None, repo_config=rc)
    tool("s1")

    assert captured["cost"] is rc
    assert captured["traces"] is rc


def test_inspect_cost_none_repo_config_uses_global(tmp_path, monkeypatch):
    """With repo_config=None the client calls receive no repo_config
    (global Secrets fallback path)."""
    s = _settings(tmp_path)
    captured = {}

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, repo_config=None: (
            captured.update(cost=repo_config) or 0.0
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, sid, repo_config=None: (
            captured.update(traces=repo_config) or []
        ),
    )

    tool = make_cost_inspect_tool(s)
    tool("s1")

    assert captured["cost"] is None
    assert captured["traces"] is None


def test_langfuse_inspect_trace_resolves_repo_credentials(tmp_path, monkeypatch):
    """make_langfuse_inspect_tool forwards repo_config to
    fetch_trace_detail and run_trace_inspector when supplied."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    rc = _repo_config()
    captured = {}

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: (
            captured.update(detail=repo_config)
            or {"id": tid, "name": "test", "observations": []}
        ),
    )

    from robotsix_mill.agents.trace_inspector import TraceInspectResult

    def fake_run_trace_inspector(**kwargs):
        captured["inspector"] = kwargs.get("repo_config")
        return TraceInspectResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        fake_run_trace_inspector,
    )

    tool = make_langfuse_inspect_tool(s, repo_dir=None, repo_config=rc)
    asyncio.run(tool("trace-1"))

    assert captured["detail"] is rc
    assert captured["inspector"] is rc


def test_langfuse_inspect_trace_none_repo_config(tmp_path, monkeypatch):
    """With repo_config=None, fetch_trace_detail and run_trace_inspector
    receive None (global fallback path)."""
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")
    captured = {}

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid, repo_config=None: (
            captured.update(detail=repo_config)
            or {"id": tid, "name": "test", "observations": []}
        ),
    )

    from robotsix_mill.agents.trace_inspector import TraceInspectResult

    def fake_run_trace_inspector(**kwargs):
        captured["inspector"] = kwargs.get("repo_config")
        return TraceInspectResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.trace_inspector.run_trace_inspector",
        fake_run_trace_inspector,
    )

    tool = make_langfuse_inspect_tool(s)
    asyncio.run(tool("trace-1"))

    assert captured["detail"] is None
    assert captured["inspector"] is None
