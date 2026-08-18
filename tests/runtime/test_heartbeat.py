"""Tests for the crash-diagnostic heartbeat module."""

from __future__ import annotations

import json

from robotsix_mill.runtime import heartbeat
from robotsix_mill.runtime.heartbeat import (
    HEARTBEAT_FILENAME,
    check_previous_death,
    mark_clean_shutdown,
    read_heartbeat,
    write_heartbeat,
)


def _read_marker(tmp_path):
    return json.loads((tmp_path / HEARTBEAT_FILENAME).read_text(encoding="utf-8"))


class TestWriteHeartbeat:
    def test_writes_running_marker(self, tmp_path):
        write_heartbeat(tmp_path)

        marker = _read_marker(tmp_path)
        assert marker["state"] == "running"
        assert marker["pid"] > 0
        assert "updated_at" in marker

    def test_creates_data_dir(self, tmp_path):
        nested = tmp_path / "a" / "b"
        write_heartbeat(nested)

        assert (nested / HEARTBEAT_FILENAME).exists()


class TestMarkCleanShutdown:
    def test_flips_state_to_stopped(self, tmp_path):
        write_heartbeat(tmp_path)
        mark_clean_shutdown(tmp_path)

        assert _read_marker(tmp_path)["state"] == "stopped"


class TestReadHeartbeat:
    def test_absent_returns_none(self, tmp_path):
        assert read_heartbeat(tmp_path) is None

    def test_corrupt_returns_none(self, tmp_path):
        (tmp_path / HEARTBEAT_FILENAME).write_text("{not json", encoding="utf-8")

        assert read_heartbeat(tmp_path) is None

    def test_non_dict_returns_none(self, tmp_path):
        (tmp_path / HEARTBEAT_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")

        assert read_heartbeat(tmp_path) is None

    def test_roundtrip(self, tmp_path):
        write_heartbeat(tmp_path)

        assert read_heartbeat(tmp_path)["state"] == "running"


class TestCheckPreviousDeath:
    def test_first_boot_returns_none(self, tmp_path):
        assert check_previous_death(tmp_path) is None

    def test_clean_shutdown_returns_none(self, tmp_path):
        write_heartbeat(tmp_path)
        mark_clean_shutdown(tmp_path)

        assert check_previous_death(tmp_path) is None

    def test_running_marker_reports_abrupt_death(self, tmp_path):
        write_heartbeat(tmp_path)

        note = check_previous_death(tmp_path)
        assert note is not None
        assert "died abruptly" in note
        assert "no clean shutdown marker" in note

    def test_corrupt_marker_treated_as_first_boot(self, tmp_path):
        (tmp_path / HEARTBEAT_FILENAME).write_text("garbage", encoding="utf-8")

        assert check_previous_death(tmp_path) is None

    def test_oom_kill_counter_adds_oom_hint(self, tmp_path, monkeypatch):
        write_heartbeat(tmp_path)
        monkeypatch.setattr(heartbeat, "_oom_kill_count", lambda: 3)

        note = check_previous_death(tmp_path)
        assert note is not None
        assert "suspected OOM kill" in note

    def test_oom_kill_zero_no_hint(self, tmp_path, monkeypatch):
        write_heartbeat(tmp_path)
        monkeypatch.setattr(heartbeat, "_oom_kill_count", lambda: 0)

        note = check_previous_death(tmp_path)
        assert note is not None
        assert "suspected OOM" not in note
