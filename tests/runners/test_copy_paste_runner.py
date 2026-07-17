"""Tests for the copy-paste entry point on :mod:`robotsix_mill.runners.periodic_runner`.

The ``run_copy_paste_pass`` callable (and all other ``run_*_pass``
callables) are factory-generated wrappers around the single
``run_periodic_pass_entry`` entry point.  This file pins the
contract: which config it selects, that ``repo_config`` flows
through unmodified, and that ``repo_config=None`` raises the
ValueError dictated by the periodic-runner contract.
"""

from __future__ import annotations

import pytest

from robotsix_mill.runners import periodic_runner
from robotsix_mill.config import RepoConfig
from robotsix_mill.runners.periodic_runner import (
    PeriodicPassResult,
    PERIODIC_PASS_CONFIGS,
)


def _repo_config() -> RepoConfig:
    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def test_run_copy_paste_pass_delegates_to_run_periodic_pass(monkeypatch):
    """Happy path: ``run_copy_paste_pass`` constructs ``Settings()``,
    selects the ``"copy_paste"`` periodic-pass config, and forwards
    the result of ``run_periodic_pass`` unchanged."""
    captured: dict = {}
    rc = _repo_config()

    def fake_run_periodic_pass(session_id, repo_config, *, config, settings):
        captured["session_id"] = session_id
        captured["repo_config"] = repo_config
        captured["config"] = config
        captured["settings"] = settings
        return PeriodicPassResult(
            updated_memory="m",
            drafts_created=[],
            session_id=session_id,
        )

    monkeypatch.setattr(periodic_runner, "run_periodic_pass", fake_run_periodic_pass)

    result = periodic_runner.run_copy_paste_pass(session_id="s1", repo_config=rc)

    assert isinstance(result, PeriodicPassResult)
    assert result.session_id == "s1"
    assert captured["session_id"] == "s1"
    # The stub MUST select the copy_paste config from the registry —
    # if it ever drifted to a different label we'd silently file
    # tickets under the wrong source kind.
    assert captured["config"] is PERIODIC_PASS_CONFIGS["copy_paste"]


def test_run_copy_paste_pass_passes_repo_config_through(monkeypatch):
    """``repo_config`` is forwarded verbatim — no rewriting, no
    fabrication, no fallback registry lookup inside the stub."""
    captured: dict = {}
    rc = _repo_config()

    def fake_run_periodic_pass(session_id, repo_config, *, config, settings):
        captured["repo_config"] = repo_config
        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id,
        )

    monkeypatch.setattr(periodic_runner, "run_periodic_pass", fake_run_periodic_pass)

    periodic_runner.run_copy_paste_pass(session_id="s", repo_config=rc)
    assert captured["repo_config"] is rc


def test_run_copy_paste_pass_repo_config_none_raises(monkeypatch):
    """The stub does not invent a ``RepoConfig`` — when called with
    ``repo_config=None`` the underlying periodic runner contract
    raises ``ValueError`` with ``"required"``, and the stub lets it
    propagate."""
    # No monkeypatch — we want the REAL run_periodic_pass to apply its
    # repo_config=None contract. Settings() and the periodic runner
    # never reach the clone/network seam because the ValueError is
    # raised first.
    with pytest.raises(ValueError, match="required"):
        periodic_runner.run_copy_paste_pass(session_id="s", repo_config=None)


def test_run_copy_paste_pass_constructs_settings_via_default(monkeypatch):
    """The stub calls bare ``Settings()`` — verify a freshly-built
    ``Settings`` instance is passed into ``run_periodic_pass``
    (autouse ``_isolate_default_data_dir`` keeps it sandboxed)."""
    from robotsix_mill.config import Settings

    captured: dict = {}

    def fake_run_periodic_pass(session_id, repo_config, *, config, settings):
        captured["settings"] = settings
        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id,
        )

    monkeypatch.setattr(periodic_runner, "run_periodic_pass", fake_run_periodic_pass)

    periodic_runner.run_copy_paste_pass(session_id="s", repo_config=_repo_config())
    assert isinstance(captured["settings"], Settings)
