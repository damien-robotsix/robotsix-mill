"""Tests for the run-health runner — deterministic cross-board digest,
recurring-failure grouping, read-only registry access, draft dedup, and
agent-failure resilience."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from robotsix_mill.config import Settings
from robotsix_mill.core.models import SourceKind
from robotsix_mill.runners import run_health_runner as rhr


def _settings(tmp_path, **kw):
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("run_health_window_hours", 168)
    return Settings(**kw)


def _write_runs(tmp_path, board_id, entries):
    board_dir = tmp_path / board_id
    board_dir.mkdir(parents=True, exist_ok=True)
    path = board_dir / "runs.json"
    path.write_text(json.dumps(entries))
    return path


def _one_repo(monkeypatch, board_id="robotsix-mill"):
    monkeypatch.setattr(
        rhr,
        "get_repos_config",
        lambda: SimpleNamespace(repos={board_id: SimpleNamespace(board_id=board_id)}),
    )


# --- Phase-1 digest: flagging + exclusions ---------------------------------


def test_digest_flags_failures_and_excludes_healthy(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=200)).isoformat()
    entries = [
        # ERROR run -> flagged
        {
            "id": "1",
            "kind": "bc_check",
            "started_at": recent,
            "status": "error",
            "error": "JSON parse error in config/config.json",
            "summary": "",
        },
        # OK but degraded summary -> flagged
        {
            "id": "2",
            "kind": "data_dir_gc",
            "started_at": recent,
            "status": "ok",
            "summary": "No findings — 0 drafts",
        },
        # Healthy OK run -> excluded
        {
            "id": "3",
            "kind": "audit",
            "started_at": recent,
            "status": "ok",
            "summary": "Created 3 drafts: t1, t2, t3",
        },
        # Running run -> excluded (in-flight)
        {
            "id": "4",
            "kind": "survey",
            "started_at": recent,
            "status": "running",
            "summary": "",
        },
        # Error but OUT of window -> excluded
        {
            "id": "5",
            "kind": "module_curator",
            "started_at": old,
            "status": "error",
            "error": "some stale failure long ago",
        },
    ]
    _write_runs(tmp_path, "robotsix-mill", entries)
    _one_repo(monkeypatch)

    digest = rhr._build_run_health_digest(s)
    assert "bc_check" in digest  # error flagged
    assert "data_dir_gc" in digest  # degraded ok flagged
    # healthy 'audit' excluded — strip the 'data_dir_gc' token first so its
    # 'audit' substring doesn't false-match.
    assert "audit" not in digest.replace("data_dir_gc", "")
    assert "survey" not in digest  # running excluded
    assert "stale failure" not in digest  # out-of-window excluded


def test_recurring_failures_collapse_into_one_group(tmp_path, monkeypatch):
    """Two occurrences sharing (kind, normalized signature) collapse into ONE
    candidate group with count >= 2 — transient path/id specifics differ."""
    s = _settings(tmp_path)
    now = datetime.now(timezone.utc)
    t1 = (now - timedelta(hours=1)).isoformat()
    t2 = (now - timedelta(hours=2)).isoformat()
    entries = [
        {
            "id": "1",
            "kind": "bc_check",
            "started_at": t1,
            "status": "error",
            "error": "JSON parse error in /tmp/clone-abc123/config/config.json could not find expected :",
        },
        {
            "id": "2",
            "kind": "bc_check",
            "started_at": t2,
            "status": "error",
            "error": "JSON parse error in /tmp/clone-def456/config/config.json could not find expected :",
        },
    ]
    _write_runs(tmp_path, "robotsix-mill", entries)
    _one_repo(monkeypatch)

    cands = rhr._collect_candidates(s)
    assert len(cands) == 1
    assert cands[0].count == 2
    assert cands[0].kind == "bc_check"


def test_reading_registries_is_read_only(tmp_path, monkeypatch):
    """Building the digest must NOT rewrite any runs.json — in particular a
    'running' entry is not reconciled to 'error' (which a second RunRegistry
    would do)."""
    s = _settings(tmp_path)
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    entries = [
        {
            "id": "1",
            "kind": "survey",
            "started_at": recent,
            "status": "running",
            "summary": "",
        },
        {
            "id": "2",
            "kind": "bc_check",
            "started_at": recent,
            "status": "error",
            "error": "boom",
        },
    ]
    path = _write_runs(tmp_path, "robotsix-mill", entries)
    original = path.read_text()
    _one_repo(monkeypatch)

    rhr._build_run_health_digest(s)

    assert path.read_text() == original  # byte-for-byte unchanged
    data = json.loads(path.read_text())
    assert data[0]["status"] == "running"  # NOT reconciled to error


def test_missing_or_corrupt_registry_is_empty(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    # No runs.json written for this board at all.
    _one_repo(monkeypatch)
    digest = rhr._build_run_health_digest(s)
    assert "no failed or degraded runs in the window" in digest


# --- draft filing + dedup --------------------------------------------------


class _FakeService:
    def __init__(self):
        self.created = []
        self._recent = []
        self._bodies = {}

    def recent_proposals_for(self, source, limit=100):
        return self._recent

    def workspace(self, ticket):
        body = self._bodies.get(ticket.id, "")
        return SimpleNamespace(read_description=lambda: body)

    def create(self, *, title, description, source, origin_session):
        t = SimpleNamespace(
            id=f"id-{len(self.created)}", title=title, description=description
        )
        self.created.append(t)
        return t


def test_file_drafts_marker_and_dedup(monkeypatch):
    s = Settings(data_dir="/tmp/rh-test")
    svc = _FakeService()
    # An existing open run-health ticket: title + embedded gap-id marker.
    svc._recent = [
        SimpleNamespace(id="old-1", title="run-health: bc_check parse error")
    ]
    svc._bodies = {"old-1": "body\n\n<!-- run-health-gap-id: bc_parse -->"}
    monkeypatch.setattr(rhr, "TicketService", lambda settings, board_id: svc)

    result = rhr.RunHealthResult(
        draft_titles=[
            "run-health: bc_check parse error",  # dup title -> skip
            "run-health: cost_recon fetch skipped",  # dup gap-id -> skip
            "run-health: brand new failure",  # new -> filed
        ],
        draft_bodies=["b1", "b2", "b3"],
        gap_ids=["g_new1", "bc_parse", "g_new3"],
    )
    created = rhr._file_drafts(result, s, "sess-1", "robotsix-mill")
    assert len(created) == 1
    assert svc.created[0].title == "run-health: brand new failure"
    # The gap-id marker is embedded in the body for later dedup.
    assert "<!-- run-health-gap-id: g_new3 -->" in svc.created[0].description


# --- agent-failure resilience ----------------------------------------------


def test_agent_failure_returns_empty_result(monkeypatch):
    """run_run_health_pass does not propagate an exception from the agent; it
    returns an empty result with the incoming memory unchanged."""
    s = Settings(data_dir="/tmp/rh-test")

    monkeypatch.setattr(rhr, "Settings", lambda: s)
    monkeypatch.setattr(rhr, "_build_run_health_digest", lambda settings: "<digest/>")
    monkeypatch.setattr(rhr, "load_memory", lambda path: "PRE-PASS LEDGER")
    monkeypatch.setattr(rhr, "_gather_recent_proposals", lambda settings, board_id: "")
    monkeypatch.setattr(rhr, "get_repos_config", lambda: SimpleNamespace(repos={}))
    monkeypatch.setattr(rhr, "persist_memory", lambda *a, **k: None)

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rhr, "run_run_health_agent", _boom)

    result = rhr.run_run_health_pass("sess-1")
    assert result.drafts_created == []
    assert result.updated_memory == "PRE-PASS LEDGER"
    assert result.session_id == "sess-1"


def test_source_kind_run_health_exists():
    assert SourceKind.RUN_HEALTH == "run-health"
