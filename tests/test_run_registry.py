"""Tests for RunRegistry: unit tests and integration tests for GET /runs."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app
from robotsix_mill.runtime.run_registry import MAX_ENTRIES, RunRegistry


# -- helpers ------------------------------------------------------------


def _ids(entries: list[dict]) -> list[str]:
    return [e["id"] for e in entries]


def _running_ids(entries: list[dict]) -> list[str]:
    return [e["id"] for e in entries if e["status"] == "running"]


# -- unit tests ---------------------------------------------------------


class TestRunRegistry:
    def test_start_creates_running_entry(self, tmp_path: Path):
        registry = RunRegistry(tmp_path / "runs.json")
        run_id = registry.start("audit")
        entries = registry.list_all()
        assert len(entries) == 1
        e = entries[0]
        assert e["id"] == run_id
        assert e["kind"] == "audit"
        assert e["status"] == "running"
        assert e["finished_at"] is None
        assert e["error"] is None

    def test_start_persists_to_file(self, tmp_path: Path):
        path = tmp_path / "runs.json"
        registry = RunRegistry(path)
        registry.start("scout")
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["kind"] == "scout"

    def test_finish_ok_transitions(self, tmp_path: Path):
        registry = RunRegistry(tmp_path / "runs.json")
        run_id = registry.start("trace-health")
        registry.finish_ok(run_id, "all good")

        entries = registry.list_all()
        assert len(entries) == 1
        e = entries[0]
        assert e["status"] == "ok"
        assert e["summary"] == "all good"
        assert e["finished_at"] is not None
        assert e["error"] is None

    def test_finish_error_transitions(self, tmp_path: Path):
        registry = RunRegistry(tmp_path / "runs.json")
        run_id = registry.start("audit")
        registry.finish_error(run_id, "something broke")

        entries = registry.list_all()
        assert len(entries) == 1
        e = entries[0]
        assert e["status"] == "error"
        assert e["error"] == "something broke"
        assert e["finished_at"] is not None

    def test_list_all_newest_first(self, tmp_path: Path):
        registry = RunRegistry(tmp_path / "runs.json")
        a = registry.start("audit")
        time.sleep(0.01)
        b = registry.start("scout")
        time.sleep(0.01)
        c = registry.start("trace-health")

        ids = _ids(registry.list_all())
        # newest first: c, b, a
        assert ids == [c, b, a]

    def test_cap_at_max_entries(self, tmp_path: Path):
        registry = RunRegistry(tmp_path / "runs.json")
        ids = []
        for _ in range(MAX_ENTRIES + 10):
            rid = registry.start("audit")
            registry.finish_ok(rid, "ok")
            ids.append(rid)

        entries = registry.list_all()
        assert len(entries) == MAX_ENTRIES
        # newest first — the first 10 should be gone
        newest_ids = _ids(entries)
        assert newest_ids == list(reversed(ids[-MAX_ENTRIES:]))

    def test_file_round_trip(self, tmp_path: Path):
        path = tmp_path / "runs.json"
        r1 = RunRegistry(path)
        a = r1.start("audit")
        r1.finish_ok(a, "audit done")
        b = r1.start("scout")
        r1.finish_error(b, "scout failed")

        # Fresh registry reading same file
        r2 = RunRegistry(path)
        entries = r2.list_all()
        assert len(entries) == 2
        # newest first: b, a
        assert _ids(entries) == [b, a]
        assert entries[0]["kind"] == "scout"
        assert entries[0]["status"] == "error"
        assert entries[1]["kind"] == "audit"
        assert entries[1]["status"] == "ok"

    def test_load_missing_file_noop(self, tmp_path: Path):
        path = tmp_path / "nonexistent.json"
        registry = RunRegistry(path)
        assert registry.list_all() == []

    def test_load_corrupt_file_noop(self, tmp_path: Path):
        path = tmp_path / "runs.json"
        path.write_text("not json")
        registry = RunRegistry(path)
        assert registry.list_all() == []

    def test_load_reconciles_orphan_running_entries(self, tmp_path: Path):
        """A run that was 'running' when the previous process died must
        not stay 'running' forever after the next startup — the
        background thread that would have called finish_ok/error is
        dead. _load reclaims those as errored so the board doesn't lie
        about an in-flight pass that ended ages ago."""
        path = tmp_path / "runs.json"
        # Simulate a prior process that started a run and never
        # finished (status="running", finished_at=None).
        prior = [{
            "id": "orphan-1", "kind": "health",
            "started_at": "2026-05-20T21:17:14+00:00",
            "finished_at": None, "status": "running",
            "summary": "", "error": None,
        }, {
            "id": "completed-1", "kind": "audit",
            "started_at": "2026-05-20T12:58:30+00:00",
            "finished_at": "2026-05-20T13:00:00+00:00",
            "status": "ok", "summary": "5 drafts", "error": None,
        }]
        path.write_text(__import__("json").dumps(prior))

        registry = RunRegistry(path)
        entries = {e["id"]: e for e in registry.list_all()}
        # Orphan reclaimed as error with the standard message.
        assert entries["orphan-1"]["status"] == "error"
        assert entries["orphan-1"]["error"] == "interrupted by process restart"
        assert entries["orphan-1"]["finished_at"] is not None
        # Completed runs are untouched.
        assert entries["completed-1"]["status"] == "ok"
        assert entries["completed-1"]["summary"] == "5 drafts"
        # Reconciliation persisted (next load sees them already-fixed,
        # not running anymore).
        registry2 = RunRegistry(path)
        assert registry2.list_all()[1]["status"] == "error"  # newest-first, orphan is older

    def test_multiple_running_entries(self, tmp_path: Path):
        registry = RunRegistry(tmp_path / "runs.json")
        a = registry.start("audit")
        b = registry.start("scout")
        assert len(_running_ids(registry.list_all())) == 2
        registry.finish_ok(a, "ok")
        assert len(_running_ids(registry.list_all())) == 1

    def test_thread_safety_smoke(self, tmp_path: Path):
        """Smoke test: concurrent starts do not corrupt the list or file."""
        import threading

        registry = RunRegistry(tmp_path / "runs.json")
        errors = []

        def worker():
            try:
                for _ in range(20):
                    rid = registry.start("audit")
                    registry.finish_ok(rid, "ok")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        entries = registry.list_all()
        # 5 * 20 = 100, capped at MAX_ENTRIES
        assert len(entries) == MAX_ENTRIES
        assert all(e["status"] == "ok" for e in entries)


# -- integration tests --------------------------------------------------


class TestGetRunsEndpoint:
    @pytest.fixture
    def client(self, settings):
        """TestClient that gives access to the app's run_registry."""
        app = create_app(settings)
        with TestClient(app) as c:
            yield c

    def test_empty_runs(self, client):
        r = client.get("/runs")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_entries_newest_first(self, client):
        registry = client.app.state.run_registry
        a = registry.start("audit")
        registry.finish_ok(a, "audit summary")
        b = registry.start("scout")
        registry.finish_error(b, "scout error")

        r = client.get("/runs")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        assert data[0]["id"] == b
        assert data[0]["kind"] == "scout"
        assert data[0]["status"] == "error"
        assert data[0]["error"] == "scout error"
        assert data[1]["id"] == a
        assert data[1]["kind"] == "audit"
        assert data[1]["status"] == "ok"
        assert data[1]["summary"] == "audit summary"

    def test_includes_running_entries(self, client):
        registry = client.app.state.run_registry
        rid = registry.start("trace-health")
        # don't finish — stays "running"

        r = client.get("/runs")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["id"] == rid
        assert data[0]["status"] == "running"
        assert data[0]["finished_at"] is None

    def test_response_fields(self, client):
        """Every entry has all the expected top-level keys."""
        registry = client.app.state.run_registry
        rid = registry.start("audit")
        registry.finish_ok(rid, "done")

        r = client.get("/runs")
        entry = r.json()[0]
        for key in ("id", "kind", "started_at", "finished_at", "status", "summary", "error"):
            assert key in entry


class TestAuditScoutTraceHealthEndpoints:
    """Regression: the three endpoints return 202 and record runs."""

    @pytest.fixture
    def client(self, settings):
        app = create_app(settings)
        with TestClient(app) as c:
            yield c

    def test_audit_records_run(self, client, monkeypatch):
        from robotsix_mill import audit_runner

        class _R:
            drafts_created: list = [{"id": "abc", "title": "x"}]

        monkeypatch.setattr(audit_runner, "run_audit_pass", lambda: _R())

        r = client.post("/audit")
        assert r.status_code == 202
        assert r.json() == {"status": "started"}

        # Wait for the daemon thread to finish
        import time
        time.sleep(0.1)

        runs = client.get("/runs").json()
        assert len(runs) >= 1
        run = runs[0]
        assert run["kind"] == "audit"
        assert run["status"] == "ok"
        assert "Created 1 drafts: abc" in run["summary"]

    def test_scout_records_run_and_returns_202(self, client, monkeypatch):
        from robotsix_mill import scout_runner

        class _R:
            drafts_created: list = [{"id": "s1", "title": "y"}]
            updated_memory: str = "mem"

        monkeypatch.setattr(scout_runner, "run_scout_pass", lambda: _R())

        r = client.post("/scout")
        assert r.status_code == 202
        assert r.json() == {"status": "started"}

        import time
        time.sleep(0.1)

        runs = client.get("/runs").json()
        assert len(runs) >= 1
        run = runs[0]
        assert run["kind"] == "scout"
        assert run["status"] == "ok"
        assert "Created 1 drafts: s1" in run["summary"]

    def test_trace_health_records_run(self, client, monkeypatch):
        from robotsix_mill import trace_health_runner

        class _R:
            draft_created: bool = True
            unsessioned_count: int = 3
            total_traces: int = 10
            window_start: str = "2025-01-01T00:00:00Z"
            window_end: str = "2025-01-02T00:00:00Z"

        monkeypatch.setattr(
            trace_health_runner, "run_trace_health_check", lambda: _R()
        )

        r = client.post("/trace-health")
        assert r.status_code == 202
        assert r.json() == {"status": "started"}

        import time
        time.sleep(0.1)

        runs = client.get("/runs").json()
        assert len(runs) >= 1
        run = runs[0]
        assert run["kind"] == "trace-health"
        assert run["status"] == "ok"
        assert "3/10 traces unsessioned" in run["summary"]
        assert "draft created" in run["summary"]

    def test_error_run_recorded(self, client, monkeypatch):
        from robotsix_mill import audit_runner

        def _fail():
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(audit_runner, "run_audit_pass", _fail)

        r = client.post("/audit")
        assert r.status_code == 202

        import time
        time.sleep(0.1)

        runs = client.get("/runs").json()
        assert len(runs) >= 1
        run = runs[0]
        assert run["kind"] == "audit"
        assert run["status"] == "error"
        assert "simulated failure" in (run["error"] or "")
