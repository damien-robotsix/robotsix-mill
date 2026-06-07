"""Tests for runtime/deps.py — FastAPI dependency callables and utilities."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from robotsix_mill.core.models import Ticket, TicketRead
from robotsix_mill.core.states import STAGE_FOR_STATE, State
from robotsix_mill.runtime.deps import (
    enrich_ticket_read,
    get_repo_config_for,
    get_run_registry,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
    with_cost,
)


# ---------------------------------------------------------------------------
# get_service / get_worker / get_settings
# ---------------------------------------------------------------------------


def test_get_service_returns_app_state_attribute():
    req = MagicMock()
    req.app.state.service = sentinel = object()
    assert get_service(req) is sentinel


def test_get_worker_returns_app_state_attribute():
    req = MagicMock()
    req.app.state.worker = sentinel = object()
    assert get_worker(req) is sentinel


def test_get_settings_returns_app_state_attribute():
    req = MagicMock()
    req.app.state.settings = sentinel = object()
    assert get_settings(req) is sentinel


# ---------------------------------------------------------------------------
# get_run_registry
# ---------------------------------------------------------------------------


def test_get_run_registry_no_repo_id_returns_default():
    req = MagicMock()
    default_registry = object()
    req.app.state.run_registry = default_registry
    # No run_registries attribute at all — getattr with fallback {}.
    result = get_run_registry(req)
    assert result is default_registry


def test_get_run_registry_with_repo_id_returns_per_board_registry():
    req = MagicMock()
    default_registry = object()
    per_board_registry = object()
    req.app.state.run_registry = default_registry
    req.app.state.run_registries = {"board-a": per_board_registry}
    repo_config = MagicMock(board_id="board-a")
    req.app.state.repos.repos = {"repo-a": repo_config}
    result = get_run_registry(req, repo_id="repo-a")
    assert result is per_board_registry


def test_get_run_registry_repo_id_unknown_board_falls_back():
    req = MagicMock()
    default_registry = object()
    req.app.state.run_registry = default_registry
    req.app.state.run_registries = {}
    repo_config = MagicMock(board_id="board-a")
    req.app.state.repos.repos = {"repo-a": repo_config}
    # board-a not in registries → fallback
    result = get_run_registry(req, repo_id="repo-a")
    assert result is default_registry


def test_get_run_registry_repo_id_not_found_falls_back():
    req = MagicMock()
    default_registry = object()
    req.app.state.run_registry = default_registry
    req.app.state.run_registries = {"board-a": object()}
    req.app.state.repos.repos = {}
    # repo-a not in repos at all → rc is None → fallback
    result = get_run_registry(req, repo_id="repo-a")
    assert result is default_registry


def test_get_run_registry_synthetic_meta_board_resolves():
    """The synthetic "meta" board is absent from repos.repos but has a
    dedicated registry keyed directly by its board id."""
    req = MagicMock()
    default_registry = object()
    meta_registry = object()
    req.app.state.run_registry = default_registry
    req.app.state.run_registries = {"meta": meta_registry}
    # "meta" is deliberately not registered as a real repo.
    req.app.state.repos.repos = {}
    result = get_run_registry(req, repo_id="meta")
    assert result is meta_registry


# ---------------------------------------------------------------------------
# get_repo_config_for
# ---------------------------------------------------------------------------


def test_get_repo_config_for_none_repo_id_returns_none():
    assert get_repo_config_for(repo_id=None) is None


def test_get_repo_config_for_known_repo():
    repos = MagicMock()
    repo_config = MagicMock()
    repos.repos = {"test-repo": repo_config}
    result = get_repo_config_for(repo_id="test-repo", repos=repos)
    assert result is repo_config


def test_get_repo_config_for_unknown_repo_raises_400():
    repos = MagicMock()
    repos.repos = {}
    with pytest.raises(HTTPException) as exc_info:
        get_repo_config_for(repo_id="unknown", repos=repos)
    assert exc_info.value.status_code == 400
    assert "unknown" in exc_info.value.detail


# ---------------------------------------------------------------------------
# maybe_enqueue
# ---------------------------------------------------------------------------


def test_maybe_enqueue_state_in_stage_for_state_calls_enqueue():
    """Any state mapped in STAGE_FOR_STATE triggers worker.enqueue."""
    for state in STAGE_FOR_STATE:
        ticket = MagicMock()
        ticket.state = state
        worker = MagicMock()
        maybe_enqueue(ticket, worker)
        worker.enqueue.assert_called_once_with(ticket.id)


def test_maybe_enqueue_state_not_in_stage_for_state_is_noop():
    """States like BLOCKED, CLOSED, AWAITING_USER_REPLY have no stage."""
    ticket = MagicMock()
    ticket.state = State.BLOCKED
    worker = MagicMock()
    maybe_enqueue(ticket, worker)
    worker.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# enrich_ticket_read
# ---------------------------------------------------------------------------


def _make_ticket(**overrides) -> Ticket:
    kwargs = {"id": "t1", "title": "Test", "workspace_path": "tasks/t1"}
    kwargs.update(overrides)
    return Ticket(**kwargs)


# ---------------------------------------------------------------------------
# with_cost — effective (post-redraft) cost
# ---------------------------------------------------------------------------


def test_with_cost_blocking_subtracts_pre_redraft_baseline(monkeypatch):
    """The blocking branch sets cost_usd to max(0, session_total -
    pre_redraft_cost_usd)."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost", lambda *a, **kw: 10.0
    )
    ticket = _make_ticket(pre_redraft_cost_usd=4.0)
    with_cost(ticket, MagicMock(), blocking=True)
    assert ticket.cost_usd == 6.0


def test_with_cost_blocking_clamped_at_zero(monkeypatch):
    """When the baseline exceeds the live total the effective cost is
    clamped to 0.0 rather than going negative."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost", lambda *a, **kw: 3.0
    )
    ticket = _make_ticket(pre_redraft_cost_usd=8.0)
    with_cost(ticket, MagicMock(), blocking=True)
    assert ticket.cost_usd == 0.0


def test_with_cost_cache_only_subtracts_pre_redraft_baseline(monkeypatch):
    """The cache-only (non-blocking) branch also subtracts the baseline."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost_cached", lambda sid: 9.0
    )
    ticket = _make_ticket(pre_redraft_cost_usd=2.5)
    with_cost(ticket, MagicMock(), blocking=False)
    assert ticket.cost_usd == 6.5


def test_enrich_ticket_read_cost_enrichment(monkeypatch):
    """enrich_ticket_read calls with_cost and the result ends up in
    TicketRead.cost_usd."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: setattr(
            ticket, "cost_usd", 1.23
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket()
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service)
    assert isinstance(result, TicketRead)
    assert result.cost_usd == 1.23


def test_enrich_ticket_read_pr_url_resolved(monkeypatch):
    """When fetch_pr_url=True, _pr_url is called and stored."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._pr_url",
        lambda ticket, settings, repo_config: "https://pr.example.com/42",
    )

    ticket = _make_ticket()
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service, fetch_pr_url=True)
    assert result.pr_url == "https://pr.example.com/42"


def test_enrich_ticket_read_pr_url_skipped_when_fetch_pr_url_false(monkeypatch):
    """When fetch_pr_url=False, _pr_url is not called."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )

    pr_url_calls = []

    def tracking_pr_url(ticket, settings, repo_config=None):
        pr_url_calls.append(1)
        return "https://pr.example.com/42"

    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", tracking_pr_url)

    ticket = _make_ticket()
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service, fetch_pr_url=False)
    assert result.pr_url is None
    assert pr_url_calls == []


def test_enrich_ticket_read_unmet_dependencies(monkeypatch):
    """unmet_deps is forwarded from service.unmet_dependencies."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket()
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = ["dep-1", "dep-2"]

    result = enrich_ticket_read(ticket, settings, service)
    assert result.unmet_deps == ["dep-1", "dep-2"]


def test_enrich_ticket_read_cumulative_cost_when_children_exist(monkeypatch):
    """When a ticket has children, cumulative_cost is computed."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: setattr(
            ticket, "cost_usd", 1.0
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket()
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = [MagicMock()]
    service.cumulative_cost.return_value = 5.0
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service)
    assert result.cumulative_cost == 5.0


def test_enrich_ticket_read_cumulative_cost_none_when_not_higher(monkeypatch):
    """cumulative_cost is only exposed when it exceeds direct cost."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: setattr(
            ticket, "cost_usd", 10.0
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket()
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = [MagicMock()]
    service.cumulative_cost.return_value = 5.0  # less than direct 10.0
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service)
    assert result.cumulative_cost is None


def test_enrich_ticket_read_parent_title_resolved(monkeypatch):
    """parent_title is resolved from parent ticket when parent_id is set."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(parent_id="parent-1")
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []
    parent = MagicMock()
    parent.title = "Parent Ticket"
    service.get.return_value = parent

    result = enrich_ticket_read(ticket, settings, service)
    assert result.parent_title == "Parent Ticket"
    service.get.assert_called_once_with("parent-1")


def test_enrich_ticket_read_dependencies_resolved(monkeypatch):
    """depends_on JSON list is resolved to [{id, title, state}]."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(depends_on='["dep-1", "dep-2"]')
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    dep1 = MagicMock()
    dep1.title = "Dep One"
    dep1.state = State.DRAFT
    dep2 = MagicMock()
    dep2.title = "Dep Two"
    dep2.state = State.DONE

    service.get.side_effect = lambda tid: {"dep-1": dep1, "dep-2": dep2}.get(tid)

    result = enrich_ticket_read(ticket, settings, service)
    assert result.dependencies == [
        {"id": "dep-1", "title": "Dep One", "state": "draft"},
        {"id": "dep-2", "title": "Dep Two", "state": "done"},
    ]


def test_enrich_ticket_read_origin_session_url(monkeypatch):
    """origin_session_url is set from _origin_session_url helper."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url",
        lambda ticket, settings, repo_config: (
            "https://langfuse.example.com/sessions/abc"
        ),
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(origin_session="sess-abc")
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service)
    assert result.origin_session_url == "https://langfuse.example.com/sessions/abc"


def test_enrich_ticket_read_depends_on_none(monkeypatch):
    """When depends_on is None, dependencies list is empty (no crash)."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(depends_on=None)
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service)
    assert result.dependencies == []


def test_enrich_ticket_read_depends_on_invalid_json(monkeypatch):
    """When depends_on contains invalid JSON, dependencies is empty (no crash)."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(depends_on="this is not json")
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    result = enrich_ticket_read(ticket, settings, service)
    assert result.dependencies == []


def test_enrich_ticket_read_blocking_cost_passed_through(monkeypatch):
    """blocking_cost is forwarded to with_cost."""
    captured = {}

    def tracking_with_cost(ticket, settings, *, blocking, repo_config):
        captured["blocking"] = blocking

    monkeypatch.setattr("robotsix_mill.runtime.deps.with_cost", tracking_with_cost)
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket()
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []

    enrich_ticket_read(ticket, settings, service, blocking_cost=False)
    assert captured["blocking"] is False

    enrich_ticket_read(ticket, settings, service, blocking_cost=True)
    assert captured["blocking"] is True
