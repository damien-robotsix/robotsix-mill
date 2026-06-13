"""Unit tests for ``resolve_mill_service`` in ``draft_target.py``.

``resolve_mill_service`` is a read-only config lookup that resolves the
mill maintenance board's :class:`TicketService` from settings, returning
``None`` on every failure path so callers fall back explicitly. These
seam-level tests cover each error path plus the success case.

(``looks_like_mill_internal`` from the same module is covered separately
in ``tests/stages/test_retrospect_stage.py``.)
"""

import logging
from types import SimpleNamespace

from robotsix_mill.core.service import TicketService
from robotsix_mill.core.draft_target import resolve_mill_service


def test_resolve_mill_service_unset_target_returns_none(settings, service, caplog):
    """Unset ``trace_review_target_repo_id`` → warn and return None."""
    settings.trace_review_target_repo_id = ""
    with caplog.at_level(logging.WARNING):
        result = resolve_mill_service(settings, service, caller_label="retrospect")
    assert result is None
    assert "trace_review_target_repo_id is unset" in caplog.text
    assert "retrospect" in caplog.text


def test_resolve_mill_service_lookup_exception_returns_none(
    settings, service, monkeypatch, caplog
):
    """A config-lookup exception (e.g. missing repos.yaml) is caught and
    yields None."""
    settings.trace_review_target_repo_id = "robotsix-mill"

    def _boom():
        raise RuntimeError("repos.yaml missing")

    monkeypatch.setattr("robotsix_mill.config.get_repos_config", _boom)
    with caplog.at_level(logging.ERROR):
        result = resolve_mill_service(settings, service, caller_label="audit")
    assert result is None
    assert "target-repo lookup failed" in caplog.text
    assert "audit" in caplog.text


def test_resolve_mill_service_unknown_repo_id_returns_none(
    settings, service, monkeypatch, caplog
):
    """A ``target_repo_id`` absent from the registry → warn and return None."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    reg = SimpleNamespace(repos={})
    monkeypatch.setattr("robotsix_mill.config.get_repos_config", lambda: reg)
    with caplog.at_level(logging.WARNING):
        result = resolve_mill_service(settings, service)
    assert result is None
    assert "not in repos.yaml" in caplog.text


def test_resolve_mill_service_missing_board_id_returns_none(
    settings, service, monkeypatch, caplog
):
    """A target repo whose config has no ``board_id`` → warn and return None."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    rc = SimpleNamespace(board_id="")
    reg = SimpleNamespace(repos={"robotsix-mill": rc})
    monkeypatch.setattr("robotsix_mill.config.get_repos_config", lambda: reg)
    with caplog.at_level(logging.WARNING):
        result = resolve_mill_service(settings, service)
    assert result is None
    assert "has no board_id" in caplog.text


def test_resolve_mill_service_success_returns_bound_service(
    settings, service, monkeypatch
):
    """A valid target repo → a fresh ``TicketService`` bound to its board."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    rc = SimpleNamespace(board_id="mill-board")
    reg = SimpleNamespace(repos={"robotsix-mill": rc})
    monkeypatch.setattr("robotsix_mill.config.get_repos_config", lambda: reg)
    result = resolve_mill_service(settings, service)
    assert isinstance(result, TicketService)
    assert result.board_id == "mill-board"
    assert result is not service
