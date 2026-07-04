"""Tests for the trace-health runner, CLI, API endpoint, and
Langfuse client pagination."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.runtime.api import create_app
from robotsix_mill.runners.trace_health_runner import (
    run_trace_health_check,
    TraceHealthResult,
)
from robotsix_mill.langfuse.client import list_all_traces_since


def _test_repo_config():
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings, repos_registry):
    """TestClient wired to the shared `settings` fixture from conftest."""
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path, **overrides):
    """Create a Settings pointed at tmp_path, with tracing enabled
    (Langfuse keys configured) so the runner doesn't short-circuit."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    # Populate Secrets so get_secrets() returns matching values
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(
        langfuse_base_url=overrides.pop("LANGFUSE_BASE_URL", "https://lf.example.com"),
        langfuse_public_key=overrides.pop("LANGFUSE_PUBLIC_KEY", "pk-test"),
        langfuse_secret_key=overrides.pop("LANGFUSE_SECRET_KEY", "sk-test"),
    )
    return __import__("robotsix_mill.config", fromlist=["Settings"]).Settings(
        **overrides
    )


def _init_db_for_test(settings):
    """Reset the cached engine so each test gets a clean, isolated DB."""
    db.reset_engine()
    db.init_db(settings, board_id="test-board")


def _enable_tracing_secrets():
    """Populate Secrets with Langfuse credentials so tracing is enabled."""
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(
        langfuse_base_url="https://lf.example.com",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _make_traces(n, with_session=True):
    """Return n synthetic trace dicts, all with or without sessionId."""
    return [
        {
            "id": f"trace-{i:03d}",
            "name": f"trace-name-{i}",
            "sessionId": f"sess-{i:03d}" if with_session else None,
        }
        for i in range(n)
    ]


def _mixed_traces(sessioned, unsessioned):
    """Return sessioned + unsessioned traces mixed together."""
    traces = []
    for i in range(sessioned):
        traces.append(
            {"id": f"s-{i:03d}", "name": f"good-{i}", "sessionId": f"sess-{i:03d}"}
        )
    for i in range(unsessioned):
        traces.append({"id": f"u-{i:03d}", "name": f"bad-{i}", "sessionId": None})
    return traces


def _mixed_traces_with_names(sessioned, unsessioned, unnamed):
    """Return sessioned + unsessioned + unnamed traces mixed together.

    Unnamed traces have a sessionId but no name (or empty name).
    """
    traces = []
    for i in range(sessioned):
        traces.append(
            {"id": f"s-{i:03d}", "name": f"good-{i}", "sessionId": f"sess-{i:03d}"}
        )
    for i in range(unsessioned):
        traces.append({"id": f"u-{i:03d}", "name": f"bad-{i}", "sessionId": None})
    for i in range(unnamed):
        traces.append(
            {"id": f"n-{i:03d}", "sessionId": f"sess-n-{i:03d}"}
        )  # no 'name' key
    return traces


def _name_orphan_traces(count, with_session=True):
    """Return *count* traces that have a sessionId but no name."""
    return [
        {"id": f"n-{i:03d}", "sessionId": f"sess-{i:03d}" if with_session else None}
        for i in range(count)
    ]


def _patch_settings(monkeypatch, settings):
    """Make run_trace_health_check use *settings* instead of its own."""
    monkeypatch.setattr(
        "robotsix_mill.runners.trace_health_runner.Settings", lambda: settings
    )


def _patch_list_all_traces(monkeypatch, traces):
    """Replace list_all_traces_since in the trace_health_runner module
    (where it's imported at module level)."""
    monkeypatch.setattr(
        "robotsix_mill.runners.trace_health_runner.list_all_traces_since",
        lambda s, ts, **kwargs: traces,
    )


# ---------------------------------------------------------------------------
# 1. Unsessoned traces → one draft
# ---------------------------------------------------------------------------


def test_unsessioned_traces_creates_draft(tmp_path, monkeypatch):
    """3 of 10 traces lack sessionId → one draft ticket created."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = _mixed_traces(sessioned=7, unsessioned=3)

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is True
    assert result.unsessioned_count == 3
    assert result.total_traces == 10

    # Exactly one ticket with source="trace-health"
    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1

    # Body assertions
    t = tickets[0]
    body = svc.workspace(t).read_description()
    assert "Unsessoned traces: 3" in body
    assert "u-000" in body
    assert "u-001" in body
    assert "u-002" in body
    assert "bad-0" in body
    # Window timestamps present (ISO 8601)
    assert "UTC →" in body
    # Title
    assert "3/10" in t.title
    assert t.state == State.DRAFT


# ---------------------------------------------------------------------------
# 2. All traces sessioned → no ticket
# ---------------------------------------------------------------------------


def test_all_sessioned_no_ticket(tmp_path, monkeypatch):
    """All 5 traces have sessionId → no draft created."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = _make_traces(5, with_session=True)

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is False
    assert result.unsessioned_count == 0
    assert result.total_traces == 5

    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 0


# ---------------------------------------------------------------------------
# 3. Zero traces → no ticket
# ---------------------------------------------------------------------------


def test_zero_traces_no_ticket(tmp_path, monkeypatch):
    """Empty trace list → no draft created."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)

    _patch_list_all_traces(monkeypatch, [])
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is False
    assert result.unsessioned_count == 0
    assert result.total_traces == 0

    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 0


# ---------------------------------------------------------------------------
# 4. Dedup: existing open trace-health ticket → skip
# ---------------------------------------------------------------------------


def test_dedup_open_ticket_skips(tmp_path, monkeypatch):
    """Pre-existing non-CLOSED trace-health ticket → no second draft."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    svc = TicketService(settings, board_id="test-board")

    # Pre-seed an open trace-health ticket (DRAFT is non-CLOSED)
    existing = svc.create("old alert", "old body", source="trace-health")
    assert existing.state == State.DRAFT  # DRAFT is non-CLOSED

    traces = _mixed_traces(sessioned=7, unsessioned=3)
    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is False
    assert result.unsessioned_count == 3

    # Still only the one pre-existing ticket
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1
    assert tickets[0].id == existing.id


def test_dedup_blocked_ticket_skips(tmp_path, monkeypatch):
    """Pre-existing BLOCKED trace-health ticket → still skip."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    svc = TicketService(settings, board_id="test-board")

    existing = svc.create("old alert", "old body", source="trace-health")
    # DRAFT → READY → BLOCKED (valid path: READY can transition to BLOCKED)
    svc.transition(existing.id, State.READY, note="auto")
    svc.transition(existing.id, State.BLOCKED, note="stuck")
    assert svc.get(existing.id).state == State.BLOCKED

    traces = _mixed_traces(sessioned=7, unsessioned=3)
    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())
    assert result.draft_created is False

    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1


# ---------------------------------------------------------------------------
# 5. Dedup: existing CLOSED trace-health ticket → still file
# ---------------------------------------------------------------------------


def test_closed_ticket_does_not_block(tmp_path, monkeypatch):
    """A CLOSED trace-health ticket → new draft still created."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    svc = TicketService(settings, board_id="test-board")

    old = svc.create("old alert", "old body", source="trace-health")
    # Valid path to CLOSED: DRAFT → READY → DELIVERABLE → IMPLEMENT_COMPLETE → HUMAN_MR_APPROVAL → DONE → CLOSED
    svc.transition(old.id, State.READY, note="auto")
    svc.transition(old.id, State.DELIVERABLE, note="auto")
    svc.transition(old.id, State.IMPLEMENT_COMPLETE, note="auto")
    svc.transition(old.id, State.HUMAN_MR_APPROVAL, note="auto")
    svc.transition(old.id, State.DONE, note="auto")
    svc.transition(old.id, State.CLOSED, note="resolved")
    assert svc.get(old.id).state == State.CLOSED

    traces = _mixed_traces(sessioned=7, unsessioned=3)
    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())
    assert result.draft_created is True

    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 2  # old closed + new draft


# ---------------------------------------------------------------------------
# 6. Langfuse unconfigured → no-op
# ---------------------------------------------------------------------------


def test_tracing_disabled_noop(tmp_path, monkeypatch):
    """When tracing_enabled is False, short-circuit before any network call."""
    from robotsix_mill.config import Settings

    settings = Settings(data_dir=str(tmp_path / "data"))
    _init_db_for_test(settings)

    # tracing_enabled is False when langfuse keys are unset (default)
    assert settings.tracing_enabled is False

    # Prove no HTTP is attempted: patch httpx.Client to raise if used
    import httpx

    captured = []

    class NoNetworkClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            captured.append("Client()")
            raise AssertionError("must not make HTTP calls when tracing disabled")

    monkeypatch.setattr(httpx, "Client", NoNetworkClient)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is False
    assert result.unsessioned_count == 0
    assert result.total_traces == 0
    assert len(captured) == 0, "httpx.Client was instantiated"

    # No tickets created
    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 0


# ---------------------------------------------------------------------------
# 7. POST /trace-health returns promptly (fire-and-forget)
# ---------------------------------------------------------------------------


def test_trace_health_endpoint_is_fire_and_forget(client, monkeypatch):
    """POST /trace-health must return 202 immediately, run in background."""
    from robotsix_mill.runners import trace_health_runner

    ran = threading.Event()
    release = threading.Event()

    def slow_check(repo_config=None):
        ran.set()
        release.wait(5)  # simulate a long run
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=0,
            total_traces=0,
            window_start="2024-01-01T00:00:00+00:00",
            window_end="2024-01-02T00:00:00+00:00",
        )

    monkeypatch.setattr(trace_health_runner, "run_trace_health_check", slow_check)

    t0 = time.monotonic()
    r = client.post("/trace-health")
    elapsed = time.monotonic() - t0

    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert elapsed < 2.0, f"response took {elapsed:.2f}s, expected <2.0s"
    assert ran.wait(5), "background thread never started"
    release.set()  # let daemon thread finish


# ---------------------------------------------------------------------------
# 8. CLI trace-health works synchronously
# ---------------------------------------------------------------------------


def test_cli_trace_health_human_output(capsys, monkeypatch):
    """CLI trace-health without --json prints human-readable summary."""
    from robotsix_mill.cli import main

    def mock_check():
        return TraceHealthResult(
            draft_created=True,
            unsessioned_count=2,
            total_traces=8,
            window_start="2024-06-01T00:00:00+00:00",
            window_end="2024-06-02T00:00:00+00:00",
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.trace_health_runner.run_trace_health_check",
        mock_check,
    )

    rc = main(["trace-health"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Trace-health check complete" in captured.out
    assert "Draft ticket created" in captured.out
    assert "Unsessoned: 2, unnamed: 0 / 8" in captured.out


def test_cli_trace_health_json_output(capsys, monkeypatch):
    """CLI trace-health --json prints valid JSON with all keys."""
    from robotsix_mill.cli import main

    def mock_check():
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=0,
            total_traces=15,
            window_start="2024-06-01T00:00:00+00:00",
            window_end="2024-06-02T00:00:00+00:00",
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.trace_health_runner.run_trace_health_check",
        mock_check,
    )

    rc = main(["trace-health", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["draft_created"] is False
    assert data["unsessioned_count"] == 0
    assert data["name_missing_count"] == 0
    assert data["total_traces"] == 15
    assert "window_start" in data
    assert "window_end" in data


def test_cli_trace_health_no_alert(capsys, monkeypatch):
    """CLI trace-health when draft_created=False shows 'No alert needed'."""
    from robotsix_mill.cli import main

    def mock_check():
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=0,
            total_traces=5,
            window_start="2024-01-01T00:00:00+00:00",
            window_end="2024-01-02T00:00:00+00:00",
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.trace_health_runner.run_trace_health_check",
        mock_check,
    )

    rc = main(["trace-health"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "No alert needed" in captured.out


def test_cli_trace_health_error(capsys, monkeypatch):
    """CLI trace-health when runner raises prints to stderr and exits 1."""
    from robotsix_mill.cli import main

    def mock_check():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "robotsix_mill.runners.trace_health_runner.run_trace_health_check",
        mock_check,
    )

    rc = main(["trace-health"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "trace-health failed: boom" in captured.err


# ---------------------------------------------------------------------------
# 9. Langfuse API pagination
# ---------------------------------------------------------------------------


def test_list_all_traces_since_pagination(monkeypatch):
    """list_all_traces_since paginates correctly across 3 pages."""
    import httpx

    # Build page responses
    pages_data = {
        1: [{"id": "a1"}, {"id": "a2"}],
        2: [{"id": "b1"}, {"id": "b2"}],
        3: [{"id": "c1"}],
    }

    call_count = []

    class FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data

        def json(self):
            return self._json

    class FakeClient:
        def __init__(self, *args, **kwargs):
            call_count.append("init")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            call_count.append(("get", dict(params)))
            page = params.get("page", 1)
            return FakeResponse(
                200,
                {
                    "data": pages_data.get(page, []),
                    "meta": {"page": page, "totalPages": 3},
                },
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)

    from robotsix_mill.config import Settings

    _enable_tracing_secrets()
    s = Settings()

    result = list_all_traces_since(s, "2024-01-01T00:00:00Z")

    assert len(result) == 5  # 2+2+1
    ids = [t["id"] for t in result]
    assert ids == ["a1", "a2", "b1", "b2", "c1"]

    # Exactly 3 GET calls (one per page)
    get_calls = [c for c in call_count if isinstance(c, tuple) and c[0] == "get"]
    assert len(get_calls) == 3
    assert get_calls[0][1]["page"] == 1
    assert get_calls[1][1]["page"] == 2
    assert get_calls[2][1]["page"] == 3


def test_list_all_traces_since_max_traces_stops_early(monkeypatch):
    """max_traces bounds the fetch: pagination stops once enough traces are
    collected, and orderBy=timestamp.desc is requested so the bounded result
    is the most recent N rather than an arbitrary slice."""
    import httpx

    pages_data = {
        1: [{"id": "n1"}, {"id": "n2"}],
        2: [{"id": "n3"}, {"id": "n4"}],
        3: [{"id": "n5"}],
    }
    get_calls = []

    class FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data

        def json(self):
            return self._json

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            get_calls.append(dict(params))
            page = params.get("page", 1)
            return FakeResponse(
                200,
                {"data": pages_data.get(page, []), "meta": {"totalPages": 3}},
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)

    from robotsix_mill.config import Settings

    _enable_tracing_secrets()
    s = Settings()

    result = list_all_traces_since(s, "2024-01-01T00:00:00Z", max_traces=3)

    # Only the first 3 collected; pagination stopped after page 2 (page 3
    # never fetched), and traces were requested newest-first.
    assert [t["id"] for t in result] == ["n1", "n2", "n3"]
    assert len(get_calls) == 2
    assert get_calls[0]["orderBy"] == "timestamp.desc"


def test_list_all_traces_since_http_error_returns_empty(monkeypatch):
    """HTTP error → returns [], logs warning."""
    import httpx

    class FakeResponse:
        status_code = 500

        def json(self):
            return {"error": "internal"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    from robotsix_mill.config import Settings

    _enable_tracing_secrets()
    s = Settings()

    result = list_all_traces_since(s, "2024-01-01T00:00:00Z")
    assert result == []


def test_list_all_traces_since_exception_returns_empty(monkeypatch):
    """Exception during fetch → returns [], logs exception."""
    import httpx

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            raise ConnectionError("boom")

    monkeypatch.setattr(httpx, "Client", FakeClient)

    from robotsix_mill.config import Settings

    _enable_tracing_secrets()
    s = Settings()

    result = list_all_traces_since(s, "2024-01-01T00:00:00Z")
    assert result == []


def test_list_all_traces_since_tracing_disabled_returns_empty():
    """When tracing_enabled=False, returns [] without any HTTP call."""
    from robotsix_mill.config import Settings

    s = Settings()  # no langfuse keys → tracing_enabled=False
    assert s.tracing_enabled is False

    result = list_all_traces_since(s, "2024-01-01T00:00:00Z")
    assert result == []


# ---------------------------------------------------------------------------
# 10. Session span wrapping
# ---------------------------------------------------------------------------


def test_start_ticket_root_span_not_called(tmp_path, monkeypatch):
    """Trace-health is a deterministic check — it must NOT call
    start_ticket_root_span, because it doesn't run an agent and
    should not create a Langfuse trace of its own.  The created
    alert ticket still has a valid origin_session."""
    from robotsix_mill.runners import trace_health_runner

    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    seen = {"span_called": False}

    def fake_start_ticket_root_span(
        sid, stage_name, extra_attributes=None, repo_config=None
    ):
        seen["span_called"] = True
        # If called, still need to yield so the body doesn't crash
        import contextlib

        return contextlib.nullcontext()

    # One unsessioned trace triggers ticket creation.
    traces = _mixed_traces(sessioned=0, unsessioned=1)

    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.start_ticket_root_span",
        fake_start_ticket_root_span,
    )
    monkeypatch.setattr(
        trace_health_runner,
        "make_session_id",
        lambda kind: f"{kind}-test-session",
    )
    monkeypatch.setattr(
        trace_health_runner,
        "list_all_traces_since",
        lambda s, ts, **kwargs: traces,
    )
    monkeypatch.setattr(
        trace_health_runner,
        "Settings",
        lambda: settings,
    )

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is True
    assert seen["span_called"] is False, (
        "start_ticket_root_span was called — trace-health must not create spans"
    )

    # Verify the created ticket's origin_session.
    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1
    assert tickets[0].origin_session is not None
    assert tickets[0].origin_session.startswith("trace-health-")


# ---------------------------------------------------------------------------
# result dataclass
# ---------------------------------------------------------------------------


def test_trace_health_result_dataclass():
    """TraceHealthResult can be constructed with all fields."""
    r = TraceHealthResult(
        draft_created=True,
        unsessioned_count=3,
        total_traces=10,
        window_start="2024-01-01T00:00:00Z",
        window_end="2024-01-02T00:00:00Z",
    )
    assert r.draft_created is True
    assert r.unsessioned_count == 3
    assert r.total_traces == 10
    assert r.window_start == "2024-01-01T00:00:00Z"
    assert r.window_end == "2024-01-02T00:00:00Z"


# ---------------------------------------------------------------------------
# 11. ValueError when repo_config is None
# ---------------------------------------------------------------------------


def test_repo_config_none_raises_value_error():
    """Calling run_trace_health_check(repo_config=None) raises ValueError."""
    with pytest.raises(ValueError, match="repo_config is required"):
        run_trace_health_check(repo_config=None)


# ---------------------------------------------------------------------------
# 12. Example cap at 5 unsessioned traces
# ---------------------------------------------------------------------------


def test_examples_capped_at_five(tmp_path, monkeypatch):
    """When >5 unsessioned traces exist, the ticket body lists exactly 5
    examples (the [:5] slice) while the title reports the real count."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = _mixed_traces(sessioned=0, unsessioned=8)

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is True
    assert result.unsessioned_count == 8
    assert result.total_traces == 8

    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1

    t = tickets[0]
    body = svc.workspace(t).read_description()

    # Title uses the real count, not the cap
    assert "8/8" in t.title

    # Body contains exactly 5 examples (u-000 … u-004)
    for i in range(5):
        assert f"u-{i:03d}" in body
    # The 6th unsessioned trace (u-005) must NOT appear
    assert "u-005" not in body


# ---------------------------------------------------------------------------
# 13. Dedup is scoped to board_id (multi-repo isolation)
# ---------------------------------------------------------------------------


def test_dedup_scoped_to_board_id(tmp_path, monkeypatch, two_repo_registry):
    """A trace-health ticket on board-a does NOT block ticket creation on
    board-b, and vice versa."""
    settings = _settings(tmp_path)

    # Initialise separate DBs for both boards
    db.reset_engine()
    db.init_db(settings, board_id="board-a")
    db.init_db(settings, board_id="board-b")

    repo_a = two_repo_registry.repos["repo-a"]
    repo_b = two_repo_registry.repos["repo-b"]

    traces = _mixed_traces(sessioned=0, unsessioned=3)
    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    # --- Direction 1: seed on board-a, run for board-b ---
    svc_a = TicketService(settings, board_id="board-a")
    existing_a = svc_a.create("alert on a", "body a", source="trace-health")

    result_b = run_trace_health_check(repo_config=repo_b)
    assert result_b.draft_created is True, (
        "board-b should not be blocked by board-a's ticket"
    )

    svc_b = TicketService(settings, board_id="board-b")
    tickets_b = [t for t in svc_b.list() if t.source == "trace-health"]
    assert len(tickets_b) == 1, "board-b should have exactly one trace-health ticket"

    # --- Direction 2: close board-a's ticket so it doesn't self-block,
    #     then run for board-a — board-b's open ticket must NOT block ---
    svc_a.transition(existing_a.id, State.READY, note="auto")
    svc_a.transition(existing_a.id, State.DELIVERABLE, note="auto")
    svc_a.transition(existing_a.id, State.IMPLEMENT_COMPLETE, note="auto")
    svc_a.transition(existing_a.id, State.HUMAN_MR_APPROVAL, note="auto")
    svc_a.transition(existing_a.id, State.DONE, note="auto")
    svc_a.transition(existing_a.id, State.CLOSED, note="resolved")

    result_a = run_trace_health_check(repo_config=repo_a)
    assert result_a.draft_created is True, (
        "board-a should not be blocked by board-b's ticket"
    )

    tickets_a = [t for t in svc_a.list() if t.source == "trace-health"]
    assert len(tickets_a) == 2  # 1 closed + 1 new draft


# ---------------------------------------------------------------------------
# 14. Name-orphan trace detection
# ---------------------------------------------------------------------------


def test_name_orphan_traces_creates_draft(tmp_path, monkeypatch):
    """All traces have sessionId but some lack name → draft created with
    name_missing_count > 0."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = _name_orphan_traces(4) + _make_traces(6, with_session=True)

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is True
    assert result.unsessioned_count == 0
    assert result.name_missing_count == 4
    assert result.total_traces == 10

    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1

    t = tickets[0]
    body = svc.workspace(t).read_description()

    # Title mentions unnamed (only-unnamed case)
    assert "4/10" in t.title
    assert "lack name" in t.title
    assert "unsessioned" not in t.title.lower()

    # Body sections
    assert "Unsessoned traces: 0" in body
    assert "Unnamed traces: 4" in body
    for i in range(4):
        assert f"n-{i:03d}" in body
        assert "(unnamed)" in body


def test_mixed_orphans_creates_draft(tmp_path, monkeypatch):
    """A mix of unsessioned, unnamed, and healthy traces → draft created,
    title covers both, body has both sections."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = _mixed_traces_with_names(sessioned=5, unsessioned=3, unnamed=2)

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is True
    assert result.unsessioned_count == 3
    assert result.name_missing_count == 2
    assert result.total_traces == 10

    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1

    t = tickets[0]
    body = svc.workspace(t).read_description()

    # Title mentions both
    assert "3 unsessioned, 2 unnamed" in t.title
    assert "/ 10 total" in t.title

    # Body sections
    assert "Unsessoned traces: 3" in body
    assert "Unnamed traces: 2" in body
    assert "u-000" in body
    assert "u-001" in body
    assert "u-002" in body
    assert "n-000" in body
    assert "(unnamed)" in body


def test_all_named_and_sessioned_no_ticket(tmp_path, monkeypatch):
    """Every trace has both name and sessionId → draft_created=False."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = _make_traces(5, with_session=True)

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is False
    assert result.unsessioned_count == 0
    assert result.name_missing_count == 0
    assert result.total_traces == 5


def test_empty_string_name_counted_as_missing(tmp_path, monkeypatch):
    """A trace with 'name': '' is counted as name-orphan."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = [
        {"id": "t1", "name": "", "sessionId": "s1"},
        {"id": "t2", "name": "good", "sessionId": "s2"},
    ]

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is True
    assert result.unsessioned_count == 0
    assert result.name_missing_count == 1
    assert result.total_traces == 2

    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1

    t = tickets[0]
    body = svc.workspace(t).read_description()
    assert "Unnamed traces: 1" in body
    assert "t1" in body
    assert "(unnamed)" in body


def test_name_orphan_examples_capped_at_five(tmp_path, monkeypatch):
    """>5 unnamed traces → body lists exactly 5."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    traces = _name_orphan_traces(8)

    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is True
    assert result.name_missing_count == 8

    svc = TicketService(settings, board_id="test-board")
    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1

    t = tickets[0]
    body = svc.workspace(t).read_description()

    # Title uses real count
    assert "8/8" in t.title

    # Body: first 5 present, 6th+ absent
    for i in range(5):
        assert f"n-{i:03d}" in body
    assert "n-005" not in body


def test_dedup_with_name_orphans_skips(tmp_path, monkeypatch):
    """An existing non-CLOSED trace-health ticket blocks a name-orphan alert."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)
    svc = TicketService(settings, board_id="test-board")

    existing = svc.create("old alert", "old body", source="trace-health")
    assert existing.state == State.DRAFT

    traces = _name_orphan_traces(3)
    _patch_list_all_traces(monkeypatch, traces)
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is False
    assert result.name_missing_count == 3

    tickets = [t for t in svc.list() if t.source == "trace-health"]
    assert len(tickets) == 1
    assert tickets[0].id == existing.id


def test_zero_traces_name_orphan_no_ticket(tmp_path, monkeypatch):
    """Empty trace list → no draft (no regression)."""
    settings = _settings(tmp_path)
    _init_db_for_test(settings)

    _patch_list_all_traces(monkeypatch, [])
    _patch_settings(monkeypatch, settings)

    result = run_trace_health_check(repo_config=_test_repo_config())

    assert result.draft_created is False
    assert result.unsessioned_count == 0
    assert result.name_missing_count == 0
    assert result.total_traces == 0
