"""Tests for the daily diagnostic agent skeleton.

Covers Settings exposure + YAML aliases, the empty-registry pass, the
pluggable check registry seam (registering a check without editing the
runner), graceful per-check failure isolation, the RunRegistry kind, and
the worker poll-loop gating flag.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from types import SimpleNamespace

from robotsix_mill.config import Settings
from robotsix_mill.runners import diagnostic_checks as dc
from robotsix_mill.runners import diagnostic_runner as dr
from robotsix_mill.runtime.run_registry import RunEntry
from robotsix_mill.runtime.worker.poll_loops import PollLoopsMixin


# --- a minimal fake check -------------------------------------------------


class _FakeCheck:
    def __init__(self, name, result=None, exc=None):
        self.name = name
        self._result = result
        self._exc = exc
        self.ran = False
        self.boards: list[str] = []

    def run(self, ctx: dc.DiagnosticCheckContext) -> dc.DiagnosticCheckResult:
        self.ran = True
        self.boards.append(ctx.board_id)
        if self._exc is not None:
            raise self._exc
        return self._result


def _fake_registry(*repo_ids):
    return SimpleNamespace(repos={r: object() for r in repo_ids}, meta=None)


def _patch_repos(monkeypatch, *repo_ids):
    """Patch ``load_repos_config`` so *repo_ids* validate as accessible."""
    import robotsix_mill.config.repos as repos_mod

    monkeypatch.setattr(
        repos_mod, "load_repos_config", lambda *a, **k: _fake_registry(*repo_ids)
    )


# --- Settings + YAML alias -------------------------------------------------


def test_settings_expose_diagnostic_fields():
    s = Settings()
    assert s.diagnostic_periodic is False
    assert s.diagnostic_interval_seconds == 86400
    assert s.diagnostic_target_repo_id == "robotsix-mill"
    assert s.diagnostic_monitored_repo_ids == []


def test_config_example_json_has_diagnostic_settings():
    import json
    from pathlib import Path

    config_path = Path(__file__).resolve().parents[2] / "config" / "config.example.json"
    with open(config_path) as fh:
        data = json.load(fh)
    settings = data["settings"]
    assert settings["diagnostic_periodic"] is False
    assert settings["diagnostic_interval_seconds"] == 86400
    assert settings["diagnostic_target_repo_id"] == "robotsix-mill"
    assert settings["diagnostic_monitored_repo_ids"] == []


# --- empty-registry pass ---------------------------------------------------


def test_empty_registry_pass_is_safe(caplog, monkeypatch):
    _patch_repos(monkeypatch, "robotsix-mill")
    # Concrete checks now self-register via import side-effect, so clear
    # the registry to exercise the empty-registry path in isolation.
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        dc.DIAGNOSTIC_CHECKS[:] = []
        assert dc.get_registered_checks() == []
        with caplog.at_level(logging.INFO, logger=dr.log.name):
            result = dr.run_diagnostic_pass("sess")
        assert isinstance(result, dr.DiagnosticPassResult)
        assert result.drafts_created == []
        assert result.summary  # non-crashing, non-empty
        messages = [r.getMessage() for r in caplog.records]
        assert any("Diagnostic pass starting" in m for m in messages)
        assert any("Diagnostic pass complete" in m for m in messages)
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


# --- pluggability ----------------------------------------------------------


def test_register_check_makes_runner_invoke_it(monkeypatch):
    _patch_repos(monkeypatch, "robotsix-mill")
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        check = _FakeCheck(
            "fake",
            result=dc.DiagnosticCheckResult(
                name="fake",
                ok=True,
                summary="ok",
                drafts_created=[{"id": "T-1", "title": "x"}],
            ),
        )
        returned = dc.register_check(check)
        assert returned is check  # usable as decorator
        result = dr.run_diagnostic_pass("sess")
        assert check.ran is True
        assert check.boards == ["robotsix-mill"]  # default single-repo board
        assert {"id": "T-1", "title": "x"} in result.drafts_created
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


def test_one_failing_check_does_not_abort_pass(monkeypatch):
    _patch_repos(monkeypatch, "robotsix-mill")
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        boom = _FakeCheck("boom", exc=RuntimeError("kaboom"))
        good = _FakeCheck(
            "good",
            result=dc.DiagnosticCheckResult(
                name="good",
                ok=True,
                summary="ok",
                drafts_created=[{"id": "T-2", "title": "y"}],
            ),
        )
        dc.register_check(boom)
        dc.register_check(good)
        # Must not raise.
        result = dr.run_diagnostic_pass("sess")
        assert boom.ran is True
        assert good.ran is True  # second check still runs
        assert {"id": "T-2", "title": "y"} in result.drafts_created
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


# --- monitored-repository list --------------------------------------------


def _register_fake(name="fake"):
    """Register a fresh ``_FakeCheck`` returning an ok result; return it."""
    check = _FakeCheck(
        name,
        result=dc.DiagnosticCheckResult(name=name, ok=True, summary="ok"),
    )
    dc.register_check(check)
    return check


def test_multiple_monitored_repos_run_each_check_once_per_repo(monkeypatch):
    _patch_repos(monkeypatch, "repo-a", "repo-b")
    monkeypatch.setenv("MILL_DIAGNOSTIC_MONITORED_REPO_IDS", '["repo-a", "repo-b"]')
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        dc.DIAGNOSTIC_CHECKS[:] = []
        check = _register_fake()
        dr.run_diagnostic_pass("sess")
        assert check.boards == ["repo-a", "repo-b"]
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


def test_empty_list_falls_back_to_target_repo(monkeypatch):
    _patch_repos(monkeypatch, "robotsix-mill")
    monkeypatch.delenv("MILL_DIAGNOSTIC_MONITORED_REPO_IDS", raising=False)
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        dc.DIAGNOSTIC_CHECKS[:] = []
        check = _register_fake()
        dr.run_diagnostic_pass("sess")
        # Empty monitored list → single-repo fallback to target_repo_id.
        assert check.boards == ["robotsix-mill"]
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


def test_invalid_repo_is_skipped_with_warning(monkeypatch, caplog):
    # Only repo-a is registered; repo-bad is unknown → skipped.
    _patch_repos(monkeypatch, "repo-a")
    monkeypatch.setenv("MILL_DIAGNOSTIC_MONITORED_REPO_IDS", '["repo-a", "repo-bad"]')
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        dc.DIAGNOSTIC_CHECKS[:] = []
        check = _register_fake()
        with caplog.at_level(logging.WARNING, logger=dr.log.name):
            dr.run_diagnostic_pass("sess")  # must not raise
        assert check.boards == ["repo-a"]  # valid repo still runs
        messages = [r.getMessage() for r in caplog.records]
        assert any("repo-bad" in m and "inaccessible" in m for m in messages)
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


def test_start_of_run_log_lists_monitored_repos(monkeypatch, caplog):
    _patch_repos(monkeypatch, "repo-a", "repo-b")
    monkeypatch.setenv("MILL_DIAGNOSTIC_MONITORED_REPO_IDS", '["repo-a", "repo-b"]')
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        dc.DIAGNOSTIC_CHECKS[:] = []
        with caplog.at_level(logging.INFO, logger=dr.log.name):
            dr.run_diagnostic_pass("sess")
        starting = [
            r.getMessage()
            for r in caplog.records
            if "Diagnostic pass starting" in r.getMessage()
        ]
        assert starting
        assert "repo-a" in starting[0] and "repo-b" in starting[0]
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


def test_load_repos_config_failure_does_not_crash(monkeypatch):
    import robotsix_mill.config.repos as repos_mod

    def _boom(*a, **k):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(repos_mod, "load_repos_config", _boom)
    monkeypatch.setenv("MILL_DIAGNOSTIC_MONITORED_REPO_IDS", '["repo-a"]')
    snapshot = list(dc.DIAGNOSTIC_CHECKS)
    try:
        dc.DIAGNOSTIC_CHECKS[:] = []
        check = _register_fake()
        # On config-load failure, fall back to attempting all configured
        # repos unvalidated rather than crashing the pass.
        result = dr.run_diagnostic_pass("sess")
        assert isinstance(result, dr.DiagnosticPassResult)
        assert check.boards == ["repo-a"]
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


# --- RunRegistry kind ------------------------------------------------------


def test_diagnostic_is_a_valid_run_kind():
    hints = typing.get_type_hints(RunEntry)
    kinds = typing.get_args(hints["kind"])
    assert "diagnostic" in kinds


# --- worker poll-loop gating ----------------------------------------------


def _fake_worker(diagnostic_periodic):
    settings = SimpleNamespace(
        diagnostic_periodic=diagnostic_periodic,
        diagnostic_interval_seconds=86400,
    )
    return SimpleNamespace(
        ctx=SimpleNamespace(settings=settings),
        _diagnostic_task=None,
    )


def test_loop_not_started_when_disabled():
    worker = _fake_worker(False)

    async def _noop():
        await asyncio.sleep(0)

    PollLoopsMixin._start_poll_loop_pass(
        worker, "diagnostic", _noop, "_diagnostic_task"
    )
    assert worker._diagnostic_task is None


def test_loop_started_when_enabled():
    async def _run():
        worker = _fake_worker(True)

        async def _noop():
            await asyncio.sleep(0)

        PollLoopsMixin._start_poll_loop_pass(
            worker, "diagnostic", _noop, "_diagnostic_task"
        )
        assert worker._diagnostic_task is not None
        worker._diagnostic_task.cancel()

    asyncio.run(_run())
