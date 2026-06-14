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
from robotsix_mill.config.loader import _YAML_PATH_TO_ALIAS
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

    def run(self) -> dc.DiagnosticCheckResult:
        self.ran = True
        if self._exc is not None:
            raise self._exc
        return self._result


# --- Settings + YAML alias -------------------------------------------------


def test_settings_expose_diagnostic_fields():
    s = Settings()
    assert s.diagnostic_periodic is False
    assert s.diagnostic_interval_seconds == 86400
    assert s.diagnostic_target_repo_id == "robotsix-mill"


def test_yaml_aliases_present():
    assert _YAML_PATH_TO_ALIAS["periodic.diagnostic.enabled"] == "diagnostic_periodic"
    assert (
        _YAML_PATH_TO_ALIAS["periodic.diagnostic.interval_seconds"]
        == "diagnostic_interval_seconds"
    )
    assert (
        _YAML_PATH_TO_ALIAS["periodic.diagnostic.target_repo_id"]
        == "diagnostic_target_repo_id"
    )


# --- empty-registry pass ---------------------------------------------------


def test_empty_registry_pass_is_safe(caplog):
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


def test_register_check_makes_runner_invoke_it():
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
        assert {"id": "T-1", "title": "x"} in result.drafts_created
    finally:
        dc.DIAGNOSTIC_CHECKS[:] = snapshot


def test_one_failing_check_does_not_abort_pass():
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
