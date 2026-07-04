"""Unit tests for the shared diagnostic data-access layer
(``runners.diagnostic_data``).

The runs-logs tests seed a real ``runs.json`` under a ``tmp_path`` data
dir; the Langfuse tests monkeypatch the delegated ``langfuse.client``
functions **in the ``diagnostic_data`` module namespace** plus
``load_repos_config`` so no real HTTP is attempted (conftest's
``_no_real_http`` autouse guard stays green).
"""

from __future__ import annotations

from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
from robotsix_mill.runners import diagnostic_data
from robotsix_mill.runtime.run_registry import RunRegistry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path):
    return Settings(data_dir=str(tmp_path / "data"))


def _seed_runs(settings, board_id, entries):
    """Seed a board's ``runs.json`` via a real ``RunRegistry``.

    *entries* is a list of ``(kind, status, started_at)`` tuples; they
    are written oldest-first so ``list_all`` returns them newest-first.
    """
    registry = RunRegistry(settings.data_dir / board_id / "runs.json")
    for kind, status, started_at in entries:
        registry.start(kind, repo_id="r")
        # Patch the just-created entry's fields directly to control
        # status/started_at deterministically.
        entry = registry._entries[-1]
        entry.status = status
        entry.started_at = started_at
    registry.flush()
    return registry


def _repo(repo_id="r", public="pk", secret="sk", base="https://lf.example.com"):
    return RepoConfig(
        repo_id=repo_id,
        langfuse_project_name="proj",
        langfuse_public_key=public,
        langfuse_secret_key=secret,
        langfuse_base_url=base,
    )


# ---------------------------------------------------------------------------
# query_runs
# ---------------------------------------------------------------------------


def test_query_runs_missing_file_returns_empty(tmp_path):
    settings = _settings(tmp_path)
    assert diagnostic_data.query_runs("no-such-board", settings=settings) == []


def test_query_runs_newest_first_no_filters(tmp_path):
    settings = _settings(tmp_path)
    _seed_runs(
        settings,
        "b",
        [
            ("diagnostic", "ok", "2026-06-10T00:00:00+00:00"),
            ("diagnostic", "error", "2026-06-11T00:00:00+00:00"),
            ("audit", "ok", "2026-06-12T00:00:00+00:00"),
        ],
    )
    runs = diagnostic_data.query_runs("b", settings=settings)
    assert [r["started_at"] for r in runs] == [
        "2026-06-12T00:00:00+00:00",
        "2026-06-11T00:00:00+00:00",
        "2026-06-10T00:00:00+00:00",
    ]


def test_query_runs_filters_kind_and_status(tmp_path):
    settings = _settings(tmp_path)
    _seed_runs(
        settings,
        "b",
        [
            ("diagnostic", "ok", "2026-06-10T00:00:00+00:00"),
            ("diagnostic", "error", "2026-06-11T00:00:00+00:00"),
            ("audit", "error", "2026-06-12T00:00:00+00:00"),
        ],
    )
    by_kind = diagnostic_data.query_runs("b", kind="diagnostic", settings=settings)
    assert {r["kind"] for r in by_kind} == {"diagnostic"}
    assert len(by_kind) == 2

    by_status = diagnostic_data.query_runs("b", status="error", settings=settings)
    assert {r["status"] for r in by_status} == {"error"}
    assert len(by_status) == 2

    both = diagnostic_data.query_runs(
        "b", kind="diagnostic", status="error", settings=settings
    )
    assert len(both) == 1
    assert both[0]["started_at"] == "2026-06-11T00:00:00+00:00"


def test_query_runs_time_window(tmp_path):
    settings = _settings(tmp_path)
    _seed_runs(
        settings,
        "b",
        [
            ("diagnostic", "ok", "2026-06-10T00:00:00+00:00"),
            ("diagnostic", "ok", "2026-06-11T00:00:00+00:00"),
            ("diagnostic", "ok", "2026-06-12T00:00:00+00:00"),
        ],
    )
    # since is inclusive, until is exclusive.
    windowed = diagnostic_data.query_runs(
        "b",
        since="2026-06-11T00:00:00+00:00",
        until="2026-06-12T00:00:00+00:00",
        settings=settings,
    )
    assert [r["started_at"] for r in windowed] == ["2026-06-11T00:00:00+00:00"]


def test_query_runs_skips_malformed_started_at_in_window(tmp_path):
    settings = _settings(tmp_path)
    registry = RunRegistry(settings.data_dir / "b" / "runs.json")
    registry.start("diagnostic", repo_id="r")
    registry._entries[-1].started_at = None  # type: ignore[assignment]
    registry.flush()
    # Malformed started_at is skipped from a time-bounded query, not raised.
    assert (
        diagnostic_data.query_runs(
            "b", since="2026-06-01T00:00:00+00:00", settings=settings
        )
        == []
    )
    # ...but is returned when no time bound is applied.
    assert len(diagnostic_data.query_runs("b", settings=settings)) == 1


# ---------------------------------------------------------------------------
# query_run_errors
# ---------------------------------------------------------------------------


def test_query_run_errors_returns_only_errors(tmp_path):
    settings = _settings(tmp_path)
    _seed_runs(
        settings,
        "b",
        [
            ("diagnostic", "ok", "2026-06-10T00:00:00+00:00"),
            ("diagnostic", "error", "2026-06-11T00:00:00+00:00"),
            ("audit", "ok", "2026-06-12T00:00:00+00:00"),
        ],
    )
    errors = diagnostic_data.query_run_errors("b", settings=settings)
    assert {r["status"] for r in errors} == {"error"}
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# Langfuse wrappers
# ---------------------------------------------------------------------------


def _patch_repos(monkeypatch, registry):
    monkeypatch.setattr(diagnostic_data, "load_repos_config", lambda *a, **k: registry)


def test_repo_config_resolution_and_passthrough(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    repo = _repo("r")
    _patch_repos(monkeypatch, ReposRegistry(repos={"r": repo}))

    captured = {}

    def fake_list_all(s, from_timestamp, repo_config):
        captured["repo_config"] = repo_config
        captured["from_timestamp"] = from_timestamp
        return [{"id": "t1", "name": "refine", "sessionId": "s1", "totalCost": 0.5}]

    monkeypatch.setattr(
        diagnostic_data.langfuse_client, "list_all_traces_since", fake_list_all
    )

    out = diagnostic_data.query_traces_since(
        "r", "2026-06-01T00:00:00Z", settings=settings
    )
    assert captured["repo_config"] is repo
    assert captured["from_timestamp"] == "2026-06-01T00:00:00Z"
    assert len(out) == 1
    assert out[0]["trace_id"] == "t1"
    assert out[0]["name"] == "refine"
    assert out[0]["session_id"] == "s1"
    assert out[0]["total_cost"] == 0.5
    assert out[0]["timestamp"] is None
    assert "observation_summary" in out[0]


def test_normalize_trace_missing_keys():
    norm = diagnostic_data._normalize_trace({})
    assert norm["trace_id"] is None
    assert norm["name"] is None
    assert norm["session_id"] is None
    assert norm["total_cost"] is None
    assert norm["timestamp"] is None
    assert "observation_summary" in norm
    assert norm["observation_summary"]["observation_count"] == 0


def test_query_recent_traces_passthrough_and_normalize(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    repo = _repo("r")
    _patch_repos(monkeypatch, ReposRegistry(repos={"r": repo}))

    captured = {}

    def fake_recent(s, *, limit, repo_config):
        captured["limit"] = limit
        captured["repo_config"] = repo_config
        return [{"id": "t9", "sessionId": "sX", "timestamp": "2026-06-13T00:00:00Z"}]

    monkeypatch.setattr(
        diagnostic_data.langfuse_client, "list_recent_traces", fake_recent
    )

    out = diagnostic_data.query_recent_traces("r", limit=3, settings=settings)
    assert captured["limit"] == 3
    assert captured["repo_config"] is repo
    assert out[0]["trace_id"] == "t9"
    assert out[0]["session_id"] == "sX"
    assert out[0]["name"] is None
    assert "observation_summary" in out[0]


def test_query_session_summary_passthrough(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    repo = _repo("r")
    _patch_repos(monkeypatch, ReposRegistry(repos={"r": repo}))

    captured = {}

    def fake_summary(s, session_id, repo_config):
        captured["session_id"] = session_id
        captured["repo_config"] = repo_config
        return "summary text"

    monkeypatch.setattr(
        diagnostic_data.langfuse_client, "fetch_session_summary", fake_summary
    )

    assert (
        diagnostic_data.query_session_summary("r", "s1", settings=settings)
        == "summary text"
    )
    assert captured["session_id"] == "s1"
    assert captured["repo_config"] is repo


def test_unknown_repo_passes_none_repo_config(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _patch_repos(monkeypatch, ReposRegistry(repos={}))

    captured = {}

    def fake_list_all(s, from_timestamp, repo_config):
        captured["repo_config"] = repo_config
        return []

    monkeypatch.setattr(
        diagnostic_data.langfuse_client, "list_all_traces_since", fake_list_all
    )

    out = diagnostic_data.query_traces_since(
        "ghost", "2026-06-01T00:00:00Z", settings=settings
    )
    assert out == []
    assert captured["repo_config"] is None


def test_langfuse_wrapper_swallows_errors(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    repo = _repo("r")
    _patch_repos(monkeypatch, ReposRegistry(repos={"r": repo}))

    def boom(*a, **k):
        raise RuntimeError("langfuse exploded")

    monkeypatch.setattr(diagnostic_data.langfuse_client, "list_all_traces_since", boom)
    monkeypatch.setattr(diagnostic_data.langfuse_client, "list_recent_traces", boom)
    monkeypatch.setattr(diagnostic_data.langfuse_client, "fetch_session_summary", boom)

    assert diagnostic_data.query_traces_since("r", "x", settings=settings) == []
    assert diagnostic_data.query_recent_traces("r", settings=settings) == []
    assert diagnostic_data.query_session_summary("r", "s1", settings=settings) is None
