"""Unit tests for PollLoopsMixin methods and helpers in poll_loops.py."""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robotsix_mill.runtime.worker.poll_loops import (
    _CI_LOG_EMBED_MAX_CHARS,
    PollLoopsMixin,
    _ci_log_body_parts,
    _dependabot_body,
    _dependabot_title,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_mixin(run_registry=None, **kwargs):
    """Create a PollLoopsMixin instance with minimal attrs."""
    m = PollLoopsMixin()
    m.run_registry = run_registry
    m.ctx = SimpleNamespace(**kwargs) if kwargs else None
    m.run_registries = {}
    return m


# ---------------------------------------------------------------------------
# _initial_delay
# ---------------------------------------------------------------------------


class TestInitialDelay:
    def test_no_registry_returns_interval_plus_jitter(self):
        mixin = _make_mixin(run_registry=None)
        result = mixin._initial_delay("test-kind", 120)
        assert result >= 120.0

    def test_never_run_returns_one_second_base(self):
        class FakeRegistry:
            def most_recent(self, kind, repo_id=None):
                return None

        mixin = _make_mixin(run_registry=None)
        reg = FakeRegistry()
        result = mixin._initial_delay("test-kind", 300, registry=reg)
        # base = 1.0, stagger_cap = max(60, min(300//12, 3600)) = max(60, 25) = 60
        # stagger in [0, 60+60) = [0, 120)
        assert result >= 1.0
        assert result < 1.0 + 60 + 60

    def test_overdue_run_returns_one_second_base(self):
        class FakeRegistry:
            def most_recent(self, kind, repo_id=None):
                return {"started_at": "2000-01-01T00:00:00+00:00"}

        mixin = _make_mixin(run_registry=None)
        reg = FakeRegistry()
        result = mixin._initial_delay("test-kind", 300, registry=reg)
        assert result >= 1.0
        assert result < 1.0 + 60 + 60

    def test_recent_run_returns_remaining_time(self):
        now = datetime.now(UTC)
        recent = now - timedelta(seconds=30)
        interval = 120

        class FakeRegistry:
            def most_recent(self, kind, repo_id=None):
                return {"started_at": recent.isoformat()}

        mixin = _make_mixin(run_registry=None)
        reg = FakeRegistry()
        result = mixin._initial_delay("test-kind", interval, registry=reg)
        expected_base = interval - 30
        # stagger_cap = max(60, min(120//12, 3600)) = max(60, 10) = 60
        assert result >= expected_base
        assert result < expected_base + 60 + 60

    def test_repo_id_scoping_passed_to_most_recent(self):
        calls = []

        class FakeRegistry:
            def most_recent(self, kind, repo_id=None):
                calls.append((kind, repo_id))

        mixin = _make_mixin(run_registry=None)
        reg = FakeRegistry()
        mixin._initial_delay("my-kind", 60, repo_id="my-repo", registry=reg)
        assert len(calls) == 1
        assert calls[0] == ("my-kind", "my-repo")

    def test_deterministic_jitter_same_kind(self):
        """Same kind → same hash-derived stagger (when random is pinned)."""

        class FakeRegistry:
            def most_recent(self, kind, repo_id=None):
                return None

        mixin = _make_mixin(run_registry=None)
        reg = FakeRegistry()
        with patch(
            "robotsix_mill.runtime.worker.poll_loops.random.uniform", return_value=0.0
        ):
            r1 = mixin._initial_delay("alpha", 300, registry=reg)
            r2 = mixin._initial_delay("alpha", 300, registry=reg)
        assert r1 == r2

    def test_deterministic_jitter_different_kinds(self):
        """Different kinds → likely different stagger offsets."""

        class FakeRegistry:
            def most_recent(self, kind, repo_id=None):
                return None

        mixin = _make_mixin(run_registry=None)
        reg = FakeRegistry()
        with patch(
            "robotsix_mill.runtime.worker.poll_loops.random.uniform", return_value=0.0
        ):
            r1 = mixin._initial_delay("alpha", 300, registry=reg)
            r2 = mixin._initial_delay("beta", 300, registry=reg)
        # Different hashes → different results (vanishingly unlikely collision)
        assert r1 != r2


# ---------------------------------------------------------------------------
# _load_ci_state
# ---------------------------------------------------------------------------


class TestLoadCiState:
    def test_file_does_not_exist_returns_defaults(self, tmp_path):
        state_path = tmp_path / "nonexistent.json"
        state, seen, deferred = PollLoopsMixin._load_ci_state(state_path)
        assert state == {"seen": {}, "deferred": {}}
        assert seen == {}
        assert deferred == {}

    def test_file_missing_keys_fills_defaults(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"other": 1}), "utf-8")
        state, seen, deferred = PollLoopsMixin._load_ci_state(state_path)
        assert state["other"] == 1
        assert state["seen"] == {}
        assert state["deferred"] == {}
        assert seen == {}
        assert deferred == {}

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("not valid json!!!", "utf-8")
        state, seen, deferred = PollLoopsMixin._load_ci_state(state_path)
        assert state == {"seen": {}, "deferred": {}}
        assert seen == {}
        assert deferred == {}

    def test_valid_json_with_seen_and_deferred(self, tmp_path):
        state_path = tmp_path / "state.json"
        data = {
            "seen": {"key1": 1234567890.0},
            "deferred": {"key2": {"n": 2, "ts": 1234567890.0}},
        }
        state_path.write_text(json.dumps(data), "utf-8")
        state, seen, deferred = PollLoopsMixin._load_ci_state(state_path)
        assert seen == {"key1": 1234567890.0}
        assert deferred == {"key2": {"n": 2, "ts": 1234567890.0}}
        assert state["seen"] is seen
        assert state["deferred"] is deferred


# ---------------------------------------------------------------------------
# _prune_ci_state
# ---------------------------------------------------------------------------


class TestPruneCiState:
    def test_old_entries_removed_from_seen(self):
        now = 100000.0
        ttl = 1000
        seen = {"old": now - 2000, "fresh": now - 100}
        deferred = {}
        PollLoopsMixin._prune_ci_state(seen, deferred, now, ttl)
        assert "old" not in seen
        assert "fresh" in seen

    def test_fresh_entries_kept(self):
        now = 100000.0
        ttl = 1000
        seen = {"a": now - 500, "b": now - 999}
        deferred = {}
        PollLoopsMixin._prune_ci_state(seen, deferred, now, ttl)
        assert seen == {"a": now - 500, "b": now - 999}

    def test_non_numeric_values_in_seen_are_kept(self):
        now = 100000.0
        ttl = 1000
        seen = {"malformed": "not-a-number", "old_num": now - 2000}
        deferred = {}
        PollLoopsMixin._prune_ci_state(seen, deferred, now, ttl)
        assert "malformed" in seen
        assert "old_num" not in seen

    def test_deferred_key_in_seen_is_removed(self):
        now = 100000.0
        ttl = 1000
        seen = {"resolved": now - 100}
        deferred = {"resolved": {"n": 2, "ts": now - 200}}
        PollLoopsMixin._prune_ci_state(seen, deferred, now, ttl)
        assert "resolved" not in deferred

    def test_deferred_non_dict_entries_removed(self):
        now = 100000.0
        ttl = 1000
        seen = {}
        deferred = {"bad": "not-a-dict", "good": {"n": 1, "ts": now - 100}}
        PollLoopsMixin._prune_ci_state(seen, deferred, now, ttl)
        assert "bad" not in deferred
        assert "good" in deferred

    def test_deferred_old_entries_removed(self):
        now = 100000.0
        ttl = 1000
        seen = {}
        deferred = {
            "old": {"n": 1, "ts": now - 2000},
            "fresh": {"n": 2, "ts": now - 500},
        }
        PollLoopsMixin._prune_ci_state(seen, deferred, now, ttl)
        assert "old" not in deferred
        assert "fresh" in deferred

    def test_fresh_deferred_entries_kept(self):
        now = 100000.0
        ttl = 1000
        seen = {}
        deferred = {"a": {"n": 1, "ts": now - 100}, "b": {"n": 2, "ts": now - 999}}
        PollLoopsMixin._prune_ci_state(seen, deferred, now, ttl)
        assert deferred == {
            "a": {"n": 1, "ts": now - 100},
            "b": {"n": 2, "ts": now - 999},
        }


# ---------------------------------------------------------------------------
# _find_canonical_ci_ticket
# ---------------------------------------------------------------------------


def _fake_ticket(tid, source, state_value, title, created_at):
    """Build a minimal fake ticket for _find_canonical_ci_ticket tests."""
    return SimpleNamespace(
        id=tid,
        source=source,
        state=SimpleNamespace(value=state_value),
        title=title,
        created_at=created_at,
    )


def _fake_service(descriptions=None, comments=None):
    """Build a fake TicketService-like object.

    *descriptions*: dict ticket_id → body string.
    *comments*: dict ticket_id → list of Comment-like objects (need created_at).
    """
    descriptions = descriptions or {}
    comments = comments or {}

    class FakeWorkspace:
        def __init__(self, desc):
            self._desc = desc

        def read_description(self):
            return self._desc

    class FakeService:
        def workspace(self, ticket):
            return FakeWorkspace(descriptions.get(ticket.id, ""))

        def list_comments(self, ticket_id):
            return comments.get(ticket_id, [])

    return FakeService()


class TestFindCanonicalCiTicket:
    def test_empty_existing_returns_none(self):
        mixin = _make_mixin()
        service = _fake_service()
        result = mixin._find_canonical_ci_ticket([], service, "wf", "main")
        assert result == (None, None)

    def test_non_ci_source_skipped(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        t = _fake_ticket(
            "t1",
            SourceKind.USER,
            State.DRAFT.value,
            "title",
            datetime.now(UTC),
        )
        service = _fake_service({"t1": "**Workflow:** wf\n**Branch:** main"})
        mixin = _make_mixin()
        result = mixin._find_canonical_ci_ticket([t], service, "wf", "main")
        assert result == (None, None)

    def test_closed_state_skipped(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        t = _fake_ticket(
            "t1", SourceKind.CI, State.CLOSED.value, "title", datetime.now(UTC)
        )
        service = _fake_service({"t1": "**Workflow:** wf\n**Branch:** main"})
        mixin = _make_mixin()
        result = mixin._find_canonical_ci_ticket([t], service, "wf", "main")
        assert result == (None, None)

    def test_done_state_skipped(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        t = _fake_ticket(
            "t1", SourceKind.CI, State.DONE.value, "title", datetime.now(UTC)
        )
        service = _fake_service({"t1": "**Workflow:** wf\n**Branch:** main"})
        mixin = _make_mixin()
        result = mixin._find_canonical_ci_ticket([t], service, "wf", "main")
        assert result == (None, None)

    def test_matching_by_body_markers_returns_ticket(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        created = datetime.now(UTC) - timedelta(minutes=10)
        t = _fake_ticket(
            "t1", SourceKind.CI, State.DRAFT.value, "Some CI failure title", created
        )
        service = _fake_service(
            {"t1": "**Workflow:** my-wf\n**Branch:** feat/a\nExtra text"},
        )
        mixin = _make_mixin()
        canonical, activity = mixin._find_canonical_ci_ticket(
            [t], service, "my-wf", "feat/a"
        )
        assert canonical is t
        assert activity == created

    def test_matching_by_title_fallback(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        created = datetime.now(UTC) - timedelta(minutes=5)
        t = _fake_ticket(
            "t2",
            SourceKind.CI,
            State.DRAFT.value,
            "Root-cause: my-wf failures on feat/b",
            created,
        )
        # Body does NOT have the markers (e.g. overwritten by refinement)
        service = _fake_service({"t2": "Refined description without markers"})
        mixin = _make_mixin()
        canonical, activity = mixin._find_canonical_ci_ticket(
            [t], service, "my-wf", "feat/b"
        )
        assert canonical is t
        assert activity == created

    def test_title_fallback_requires_both_wf_name_and_target(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        created = datetime.now(UTC)
        # Title only contains wf_name but not target → should NOT match
        t = _fake_ticket(
            "t3",
            SourceKind.CI,
            State.DRAFT.value,
            "Root-cause: my-wf failures",
            created,
        )
        service = _fake_service({"t3": "No markers here either"})
        mixin = _make_mixin()
        canonical, activity = mixin._find_canonical_ci_ticket(
            [t], service, "my-wf", "feat/c"
        )
        assert canonical is None

    def test_multiple_matches_returns_most_recent_activity(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        older = datetime.now(UTC) - timedelta(minutes=20)
        newer = datetime.now(UTC) - timedelta(minutes=5)

        t1 = _fake_ticket("t1", SourceKind.CI, State.DRAFT.value, "CI fail 1", older)
        t2 = _fake_ticket("t2", SourceKind.CI, State.DRAFT.value, "CI fail 2", newer)

        service = _fake_service(
            {
                "t1": "**Workflow:** ci\n**Branch:** main\nold failure",
                "t2": "**Workflow:** ci\n**Branch:** main\nnew failure",
            },
        )
        mixin = _make_mixin()
        canonical, activity = mixin._find_canonical_ci_ticket(
            [t1, t2], service, "ci", "main"
        )
        assert canonical is t2
        assert activity == newer

    def test_comment_activity_considered_for_most_recent(self):
        from robotsix_mill.core.models import SourceKind
        from robotsix_mill.core.states import State

        created_t1 = datetime.now(UTC) - timedelta(minutes=30)
        created_t2 = datetime.now(UTC) - timedelta(minutes=25)
        comment_time = datetime.now(UTC) - timedelta(minutes=1)

        t1 = _fake_ticket(
            "t1", SourceKind.CI, State.DRAFT.value, "CI fail old", created_t1
        )
        t2 = _fake_ticket(
            "t2", SourceKind.CI, State.DRAFT.value, "CI fail recent", created_t2
        )

        # t1 has a very recent comment → should win despite older created_at
        comments = {
            "t1": [SimpleNamespace(created_at=comment_time, body="update")],
        }

        service = _fake_service(
            {
                "t1": "**Workflow:** ci\n**Branch:** main",
                "t2": "**Workflow:** ci\n**Branch:** main",
            },
            comments=comments,
        )
        mixin = _make_mixin()
        canonical, activity = mixin._find_canonical_ci_ticket(
            [t1, t2], service, "ci", "main"
        )
        assert canonical is t1
        assert activity == comment_time


# ---------------------------------------------------------------------------
# _fetch_run_logs_with_deferral
# ---------------------------------------------------------------------------


class _FakeForge:
    def __init__(self, logs="", raise_pattern=None):
        self.logs = logs
        self.raise_pattern = raise_pattern or []
        self.fetch_calls = 0

    def fetch_workflow_job_logs(self, *, run_id):
        self.fetch_calls += 1
        idx = self.fetch_calls - 1
        if idx < len(self.raise_pattern) and self.raise_pattern[idx]:
            raise ConnectionError("simulated ConnectError")
        return self.logs


class TestFetchRunLogsWithDeferral:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        forge = _FakeForge(logs="job output")
        mixin = _make_mixin()
        deferred = {}
        now = time.time()
        (
            logs,
            error,
            deferred_flag,
            network_down,
        ) = await mixin._fetch_run_logs_with_deferral(
            forge,
            42,
            "key1",
            deferred,
            now,
            "repo",
            "wf",
        )
        assert logs == "job output"
        assert error == ""
        assert deferred_flag is False
        assert network_down is False
        assert forge.fetch_calls == 1

    @pytest.mark.asyncio
    async def test_success_after_second_attempt(self):
        forge = _FakeForge(logs="eventual output", raise_pattern=[True, False])
        mixin = _make_mixin()
        deferred = {}
        now = time.time()
        with patch(
            "robotsix_mill.runtime.worker.poll_loops.asyncio.sleep", return_value=None
        ):
            (
                logs,
                error,
                deferred_flag,
                network_down,
            ) = await mixin._fetch_run_logs_with_deferral(
                forge,
                42,
                "key1",
                deferred,
                now,
                "repo",
                "wf",
            )
        assert logs == "eventual output"
        assert error == ""
        assert deferred_flag is False
        assert network_down is False
        assert forge.fetch_calls == 2

    @pytest.mark.asyncio
    async def test_all_attempts_fail_within_deferral_limit(self):
        forge = _FakeForge(logs="", raise_pattern=[True, True, True])
        mixin = _make_mixin()
        deferred = {}
        now = 1234567890.0
        with patch(
            "robotsix_mill.runtime.worker.poll_loops.asyncio.sleep", return_value=None
        ):
            (
                logs,
                error,
                deferred_flag,
                network_down,
            ) = await mixin._fetch_run_logs_with_deferral(
                forge,
                42,
                "key1",
                deferred,
                now,
                "repo",
                "wf",
            )
        assert logs == ""
        assert "ConnectError" in error
        assert deferred_flag is True
        assert network_down is False
        assert deferred == {"key1": {"n": 1, "ts": now}}
        assert forge.fetch_calls == 3

    @pytest.mark.asyncio
    async def test_all_attempts_fail_exceeds_deferral_limit(self):
        forge = _FakeForge(logs="", raise_pattern=[True, True, True])
        mixin = _make_mixin()
        now = 1234567890.0
        # deferred already has count=3 for this key → next is 4 > max (3)
        deferred = {"key1": {"n": 3, "ts": now - 100}}
        with patch(
            "robotsix_mill.runtime.worker.poll_loops.asyncio.sleep", return_value=None
        ):
            (
                logs,
                error,
                deferred_flag,
                network_down,
            ) = await mixin._fetch_run_logs_with_deferral(
                forge,
                42,
                "key1",
                deferred,
                now,
                "repo",
                "wf",
            )
        assert logs == ""
        assert "ConnectError" in error
        assert deferred_flag is False
        assert network_down is False
        assert "key1" not in deferred
        assert forge.fetch_calls == 3

    @pytest.mark.asyncio
    async def test_retries_three_times_on_persistent_failure(self):
        forge = _FakeForge(logs="", raise_pattern=[True, True, True])
        mixin = _make_mixin()
        deferred = {}
        now = time.time()
        with patch(
            "robotsix_mill.runtime.worker.poll_loops.asyncio.sleep", return_value=None
        ):
            await mixin._fetch_run_logs_with_deferral(
                forge,
                42,
                "key1",
                deferred,
                now,
                "repo",
                "wf",
            )
        assert forge.fetch_calls == 3


# ---------------------------------------------------------------------------
# _dependabot_title
# ---------------------------------------------------------------------------


class TestDependabotTitle:
    def test_has_severity_and_package(self):
        title = _dependabot_title({"severity": "critical", "package": "requests"})
        assert title == "Dependabot: Critical vulnerability in requests"

    def test_missing_severity_defaults_to_unknown(self):
        title = _dependabot_title({"package": "lodash"})
        assert title == "Dependabot: Unknown vulnerability in lodash"

    def test_missing_package_defaults_to_dependency(self):
        title = _dependabot_title({"severity": "high"})
        assert title == "Dependabot: High vulnerability in dependency"

    def test_empty_dict(self):
        title = _dependabot_title({})
        assert title == "Dependabot: Unknown vulnerability in dependency"


# ---------------------------------------------------------------------------
# _dependabot_body
# ---------------------------------------------------------------------------


class TestDependabotBody:
    def test_full_alert_includes_all_lines(self):
        alert = {
            "package": "requests",
            "ecosystem": "pip",
            "severity": "critical",
            "ghsa_id": "GHSA-1234",
            "cve_id": "CVE-2025-0001",
            "manifest_path": "requirements.txt",
            "url": "https://github.com/o/r/security/dependabot/1",
            "summary": "A serious vulnerability was found.",
        }
        body = _dependabot_body(alert)
        assert "**Package:** `requests` (pip)" in body
        assert "**Severity:** critical" in body
        assert "**Advisory:** GHSA-1234" in body
        assert "**CVE:** CVE-2025-0001" in body
        assert "**Manifest:** `requirements.txt`" in body
        assert "**Alert:** https://github.com/o/r/security/dependabot/1" in body
        assert "A serious vulnerability was found." in body

    def test_only_required_fields(self):
        alert = {
            "package": "lodash",
            "ecosystem": "npm",
            "severity": "high",
        }
        body = _dependabot_body(alert)
        assert "**Package:** `lodash` (npm)" in body
        assert "**Severity:** high" in body
        assert "**Advisory:**" not in body
        assert "**CVE:**" not in body
        assert "**Manifest:**" not in body
        assert "**Alert:**" not in body

    def test_empty_alert_minimal_body(self):
        body = _dependabot_body({})
        assert "**Package:** `` ()" in body
        assert "**Severity:** " in body


# ---------------------------------------------------------------------------
# _ci_log_body_parts
# ---------------------------------------------------------------------------


class TestCiLogBodyParts:
    _ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    def test_short_logs_embedded_whole_without_notice(self):
        parts = _ci_log_body_parts("\x1b[31merror: boom\x1b[0m\n", self._ansi_re)
        assert parts == ["```", "error: boom\n", "```"]

    def test_long_logs_truncated_to_tail_with_notice(self):
        # Real job logs are line-oriented and much larger than the cap
        # (the 2026-09-04 incident description was ~129K chars).
        logs = "\n".join(
            f"2026-09-04T08:00:{i % 60:02d}Z step output line {i}" for i in range(5000)
        )
        assert len(logs) > _CI_LOG_EMBED_MAX_CHARS
        parts = _ci_log_body_parts(logs, self._ansi_re)
        assert parts[0].startswith("_Log tail below (truncated")
        embedded = parts[2]
        assert len(embedded) == _CI_LOG_EMBED_MAX_CHARS
        assert embedded.endswith("step output line 4999")
        assert len("\n".join(parts)) < 25_000
