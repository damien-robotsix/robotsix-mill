"""Tests for runtime/deps.py — FastAPI dependency callables and utilities."""

from unittest.mock import MagicMock

from robotsix_mill.core.models import Ticket, TicketRead
from robotsix_mill.core.states import STAGE_FOR_STATE, State
from robotsix_mill.runtime.deps import (
    _cumulative_cost_for,
    _dependencies_for,
    _origin_session_url,
    _parent_title_for,
    _parse_str_id_list,
    _pr_url,
    _pr_url_for,
    _pr_urls_for_multi_repo,
    enrich_ticket_read,
    get_broadcaster,
    get_repos_registry,
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
        "robotsix_mill.langfuse.client.session_cost_cached",
        lambda sid, **kw: 9.0,
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


# --- pending_question enrichment ---------------------------------------


def test_enrich_ticket_read_pending_question_when_paused(monkeypatch):
    """When the ticket is in AWAITING_USER_REPLY, pending_question is
    populated from service.pending_question."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(state=State.AWAITING_USER_REPLY)
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []
    service.pending_question.return_value = "What color?"

    result = enrich_ticket_read(ticket, settings, service)
    assert result.pending_question == "What color?"
    service.pending_question.assert_called_once_with(ticket.id)


def test_enrich_ticket_read_pending_question_none_when_not_paused(monkeypatch):
    """When the ticket is NOT in AWAITING_USER_REPLY, pending_question
    is None and service.pending_question is NOT called."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(state=State.DRAFT)
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []
    service.pending_question = MagicMock()

    result = enrich_ticket_read(ticket, settings, service)
    assert result.pending_question is None
    # pending_question should NOT be called for non-paused tickets
    service.pending_question.assert_not_called()


def test_enrich_ticket_read_pending_question_none_when_no_open_ask(monkeypatch):
    """When paused but no open [ASK_USER] thread, pending_question is None."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.with_cost",
        lambda ticket, settings, *, blocking, repo_config: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._origin_session_url", lambda *a, **kw: None
    )
    monkeypatch.setattr("robotsix_mill.runtime.deps._pr_url", lambda *a, **kw: None)

    ticket = _make_ticket(state=State.AWAITING_USER_REPLY)
    settings = MagicMock()
    service = MagicMock()
    service.list_children.return_value = []
    service.unmet_dependencies.return_value = []
    service.pending_question.return_value = None

    result = enrich_ticket_read(ticket, settings, service)
    assert result.pending_question is None


# ---------------------------------------------------------------------------
# get_broadcaster / get_repos_registry
# ---------------------------------------------------------------------------


def test_get_broadcaster_returns_app_state_attribute():
    req = MagicMock()
    req.app.state.broadcaster = sentinel = object()
    assert get_broadcaster(req) is sentinel


def test_get_repos_registry_returns_app_state_attribute():
    req = MagicMock()
    req.app.state.repos = sentinel = object()
    assert get_repos_registry(req) is sentinel


# ---------------------------------------------------------------------------
# _parse_str_id_list
# ---------------------------------------------------------------------------


def test_parse_str_id_list_none_returns_empty():
    assert _parse_str_id_list(None) == []


def test_parse_str_id_list_empty_string_returns_empty():
    assert _parse_str_id_list("") == []


def test_parse_str_id_list_valid_json_array():
    result = _parse_str_id_list('["a", "b", "c"]')
    assert result == ["a", "b", "c"]


def test_parse_str_id_list_invalid_json_returns_empty():
    assert _parse_str_id_list("this is not json") == []


def test_parse_str_id_list_non_list_json_returns_empty():
    assert _parse_str_id_list('{"a": 1}') == []


def test_parse_str_id_list_mixed_type_list_filters_non_strings():
    result = _parse_str_id_list('["a", 1, "b", null, true, "c"]')
    assert result == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _origin_session_url
# ---------------------------------------------------------------------------


def test_origin_session_url_none_origin_returns_none(monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing._build_langfuse_url",
        lambda *a, **kw: "https://langfuse.example.com/should-not-be-called",
    )
    ticket = _make_ticket(origin_session=None)
    result = _origin_session_url(ticket, MagicMock())
    assert result is None


def test_origin_session_url_empty_string_origin_returns_none(monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing._build_langfuse_url",
        lambda *a, **kw: "https://langfuse.example.com/should-not-be-called",
    )
    ticket = _make_ticket(origin_session="")
    result = _origin_session_url(ticket, MagicMock())
    assert result is None


def test_origin_session_url_valid_session_returns_url(monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing._build_langfuse_url",
        lambda origin, entity_type, repo_config=None: (
            f"https://langfuse.example.com/{entity_type}/{origin}"
        ),
    )
    ticket = _make_ticket(origin_session="sess-abc")
    result = _origin_session_url(ticket, MagicMock())
    assert result == "https://langfuse.example.com/sessions/sess-abc"


# ---------------------------------------------------------------------------
# _pr_url
# ---------------------------------------------------------------------------


def test_pr_url_non_review_state_returns_none(monkeypatch):
    """Ticket in DRAFT (not a review state) returns None without calling get_forge."""
    get_forge_called = []

    def tracking_get_forge(*a, **kw):
        get_forge_called.append(1)
        return MagicMock()

    monkeypatch.setattr("robotsix_mill.runtime.deps.get_forge", tracking_get_forge)

    ticket = _make_ticket(state=State.DRAFT, branch="feature/xyz")
    result = _pr_url(ticket, MagicMock())
    assert result is None
    assert get_forge_called == []


def test_pr_url_review_state_with_branch_returns_url(monkeypatch):
    """Ticket in IMPLEMENT_COMPLETE with branch set calls get_forge and returns the URL."""
    forge_mock = MagicMock()
    forge_mock.pr_status.return_value = {"url": "https://pr.example.com/42"}

    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.get_forge",
        lambda settings, repo_config=None: forge_mock,
    )

    ticket = _make_ticket(state=State.IMPLEMENT_COMPLETE, branch="feature/xyz")
    result = _pr_url(ticket, MagicMock())
    assert result == "https://pr.example.com/42"
    forge_mock.pr_status.assert_called_once_with(source_branch="feature/xyz")


def test_pr_url_review_state_branch_none_infers_from_settings(monkeypatch):
    """When branch is None, the branch is inferred from settings.branch_prefix + ticket.id."""
    forge_mock = MagicMock()
    forge_mock.pr_status.return_value = {"url": "https://pr.example.com/99"}

    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.get_forge",
        lambda settings, repo_config=None: forge_mock,
    )

    settings = MagicMock()
    settings.branch_prefix = "mill/"
    ticket = _make_ticket(id="ticket-1", state=State.IMPLEMENT_COMPLETE, branch=None)
    result = _pr_url(ticket, settings)
    assert result == "https://pr.example.com/99"
    forge_mock.pr_status.assert_called_once_with(source_branch="mill/ticket-1")


def test_pr_url_get_forge_raises_runtime_error_returns_none(monkeypatch):
    """When get_forge raises RuntimeError (forge not configured), returns None."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.get_forge",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no forge configured")),
    )
    ticket = _make_ticket(state=State.IMPLEMENT_COMPLETE, branch="feature/xyz")
    result = _pr_url(ticket, MagicMock())
    assert result is None


def test_pr_url_get_forge_raises_generic_exception_returns_none(monkeypatch):
    """When get_forge raises a generic Exception (transient), returns None."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.get_forge",
        lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("transient")),
    )
    ticket = _make_ticket(state=State.IMPLEMENT_COMPLETE, branch="feature/xyz")
    result = _pr_url(ticket, MagicMock())
    assert result is None


def test_pr_url_pr_status_missing_url_key_returns_none(monkeypatch):
    """When pr_status returns a dict without 'url', returns None."""
    forge_mock = MagicMock()
    forge_mock.pr_status.return_value = {"no_url_here": 1}

    monkeypatch.setattr(
        "robotsix_mill.runtime.deps.get_forge",
        lambda settings, repo_config=None: forge_mock,
    )

    ticket = _make_ticket(state=State.IMPLEMENT_COMPLETE, branch="feature/xyz")
    result = _pr_url(ticket, MagicMock())
    assert result is None


# ---------------------------------------------------------------------------
# _pr_urls_for_multi_repo
# ---------------------------------------------------------------------------


def test_pr_urls_for_multi_repo_file_does_not_exist(tmp_path):
    """When pr_urls.json does not exist, returns None."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    # File not created — path.exists() returns False
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result is None


def test_pr_urls_for_multi_repo_single_url(tmp_path):
    """Valid file with one URL returns that URL as a string."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    (tmp_path / "pr_urls.json").write_text('[{"url": "https://pr.example.com/1"}]')
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result == "https://pr.example.com/1"


def test_pr_urls_for_multi_repo_multiple_urls(tmp_path):
    """Valid file with multiple URLs returns comma-joined string."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    (tmp_path / "pr_urls.json").write_text(
        '[{"url": "https://pr.example.com/1"}, {"url": "https://pr.example.com/2"}]'
    )
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result == "https://pr.example.com/1, https://pr.example.com/2"


def test_pr_urls_for_multi_repo_invalid_json(tmp_path):
    """File with invalid JSON returns None (no crash)."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    (tmp_path / "pr_urls.json").write_text("this is not json {{{")
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result is None


def test_pr_urls_for_multi_repo_non_list_json(tmp_path):
    """File whose JSON is not a list returns None."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    (tmp_path / "pr_urls.json").write_text('{"a": 1}')
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result is None


def test_pr_urls_for_multi_repo_empty_list(tmp_path):
    """File with an empty JSON list returns None."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    (tmp_path / "pr_urls.json").write_text("[]")
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result is None


def test_pr_urls_for_multi_repo_missing_url_key_skipped(tmp_path):
    """List items without a 'url' key are skipped; if all skipped → None."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    (tmp_path / "pr_urls.json").write_text(
        '[{"not_url": "x"}, {"url": "https://pr.example.com/ok"}]'
    )
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result == "https://pr.example.com/ok"


def test_pr_urls_for_multi_repo_all_missing_url_key_returns_none(tmp_path):
    """When all list items are missing 'url', returns None."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    (tmp_path / "pr_urls.json").write_text('[{"not_url": "x"}, {"also_not": "y"}]')
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result is None


def test_pr_urls_for_multi_repo_truncation_at_1000_chars(tmp_path):
    """Joined string longer than 1000 chars is truncated."""
    ticket = _make_ticket()
    service = MagicMock()
    ws = MagicMock()
    ws.artifacts_dir = tmp_path
    service.workspace.return_value = ws
    # Create a URL long enough that joining them exceeds 1000 chars
    long_url = "https://pr.example.com/" + ("x" * 990)
    (tmp_path / "pr_urls.json").write_text(
        '[{"url": "' + long_url + '"}, {"url": "https://pr.example.com/2"}]'
    )
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result is not None
    assert len(result) == 1000


def test_pr_urls_for_multi_repo_workspace_raises_oserror():
    """When service.workspace() raises OSError, returns None (no crash)."""
    ticket = _make_ticket()
    service = MagicMock()
    service.workspace.side_effect = OSError("boom")
    result = _pr_urls_for_multi_repo(ticket, service)
    assert result is None


# ---------------------------------------------------------------------------
# _cumulative_cost_for
# ---------------------------------------------------------------------------


def test_cumulative_cost_for_no_children_returns_none():
    """When the ticket has no children, returns None."""
    ticket = _make_ticket(cost_usd=5.0)
    service = MagicMock()
    service.list_children.return_value = []
    result = _cumulative_cost_for(
        ticket, MagicMock(), service, blocking_cost=True, repo_config=None
    )
    assert result is None


def test_cumulative_cost_for_cumulative_not_higher_returns_none():
    """When cumulative cost does not exceed direct cost, returns None."""
    ticket = _make_ticket(cost_usd=10.0)
    service = MagicMock()
    service.list_children.return_value = [MagicMock()]
    service.cumulative_cost.return_value = 5.0  # ≤ direct cost
    result = _cumulative_cost_for(
        ticket, MagicMock(), service, blocking_cost=True, repo_config=None
    )
    assert result is None


def test_cumulative_cost_for_cumulative_higher_returns_cumulative():
    """When cumulative cost exceeds direct cost, returns cumulative."""
    ticket = _make_ticket(cost_usd=3.0)
    service = MagicMock()
    service.list_children.return_value = [MagicMock()]
    service.cumulative_cost.return_value = 12.0
    result = _cumulative_cost_for(
        ticket, MagicMock(), service, blocking_cost=True, repo_config=None
    )
    assert result == 12.0


# ---------------------------------------------------------------------------
# _parent_title_for
# ---------------------------------------------------------------------------


def test_parent_title_for_parent_id_none_returns_none():
    """When parent_id is None, returns None."""
    ticket = _make_ticket(parent_id=None)
    result = _parent_title_for(ticket, MagicMock())
    assert result is None


def test_parent_title_for_parent_not_found_returns_none():
    """When parent_id is set but service.get returns None, returns None."""
    ticket = _make_ticket(parent_id="parent-1")
    service = MagicMock()
    service.get.return_value = None
    result = _parent_title_for(ticket, service)
    assert result is None
    service.get.assert_called_once_with("parent-1")


def test_parent_title_for_parent_found_returns_title():
    """When parent is found, returns its title."""
    ticket = _make_ticket(parent_id="parent-1")
    service = MagicMock()
    parent = MagicMock()
    parent.title = "Parent Ticket Title"
    service.get.return_value = parent
    result = _parent_title_for(ticket, service)
    assert result == "Parent Ticket Title"


# ---------------------------------------------------------------------------
# _dependencies_for
# ---------------------------------------------------------------------------


def test_dependencies_for_depends_on_none_returns_empty():
    """When depends_on is None, returns []."""
    ticket = _make_ticket(depends_on=None)
    result = _dependencies_for(ticket, MagicMock())
    assert result == []


def test_dependencies_for_depends_on_invalid_json_returns_empty():
    """When depends_on is invalid JSON, returns []."""
    ticket = _make_ticket(depends_on="not valid json {{{")
    result = _dependencies_for(ticket, MagicMock())
    assert result == []


def test_dependencies_for_depends_on_non_list_json_returns_empty():
    """When depends_on is valid JSON that is not a list, returns []."""
    ticket = _make_ticket(depends_on='{"a": 1}')
    result = _dependencies_for(ticket, MagicMock())
    assert result == []


def test_dependencies_for_non_string_items_skipped():
    """List items that are not strings are skipped."""
    ticket = _make_ticket(depends_on='["dep-1", 42, null, "dep-2"]')
    service = MagicMock()

    dep1 = MagicMock()
    dep1.title = "Dep One"
    dep1.state = State.DRAFT
    dep2 = MagicMock()
    dep2.title = "Dep Two"
    dep2.state = State.DONE

    service.get.side_effect = lambda tid: {"dep-1": dep1, "dep-2": dep2}.get(tid)

    result = _dependencies_for(ticket, service)
    assert result == [
        {"id": "dep-1", "title": "Dep One", "state": "draft"},
        {"id": "dep-2", "title": "Dep Two", "state": "done"},
    ]


def test_dependencies_for_valid_ids_resolved():
    """Valid string IDs are resolved to {id, title, state} dicts."""
    ticket = _make_ticket(depends_on='["dep-a", "dep-b"]')
    service = MagicMock()

    dep_a = MagicMock()
    dep_a.title = "Dep A"
    dep_a.state = State.BLOCKED
    dep_b = MagicMock()
    dep_b.title = "Dep B"
    dep_b.state = State.READY

    service.get.side_effect = lambda tid: {"dep-a": dep_a, "dep-b": dep_b}.get(tid)

    result = _dependencies_for(ticket, service)
    assert result == [
        {"id": "dep-a", "title": "Dep A", "state": "blocked"},
        {"id": "dep-b", "title": "Dep B", "state": "ready"},
    ]


# ---------------------------------------------------------------------------
# _pr_url_for
# ---------------------------------------------------------------------------


def test_pr_url_for_fetch_pr_url_false_returns_none(monkeypatch):
    """When fetch_pr_url=False, returns None without calling either helper."""
    multi_calls = []
    single_calls = []

    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._pr_urls_for_multi_repo",
        lambda *a, **kw: multi_calls.append(1) or "should-not-be-called",
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._pr_url",
        lambda *a, **kw: single_calls.append(1) or "should-not-be-called",
    )

    ticket = _make_ticket()
    result = _pr_url_for(
        ticket, MagicMock(), MagicMock(), fetch_pr_url=False, repo_config=None
    )
    assert result is None
    assert multi_calls == []
    assert single_calls == []


def test_pr_url_for_multi_repo_returns_value(monkeypatch):
    """When multi-repo manifest returns a value, that value is returned."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._pr_urls_for_multi_repo",
        lambda ticket, service: (
            "https://multi.example.com/1, https://multi.example.com/2"
        ),
    )
    single_calls = []
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._pr_url",
        lambda *a, **kw: single_calls.append(1) or "should-not-be-called",
    )

    ticket = _make_ticket()
    result = _pr_url_for(
        ticket, MagicMock(), MagicMock(), fetch_pr_url=True, repo_config=None
    )
    assert result == "https://multi.example.com/1, https://multi.example.com/2"
    assert single_calls == []


def test_pr_url_for_multi_repo_returns_none_falls_back_to_single(monkeypatch):
    """When multi-repo returns None, falls back to single-repo _pr_url."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._pr_urls_for_multi_repo",
        lambda ticket, service: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.deps._pr_url",
        lambda ticket, settings, repo_config=None: "https://single.example.com/42",
    )

    ticket = _make_ticket()
    settings = MagicMock()
    result = _pr_url_for(
        ticket, settings, MagicMock(), fetch_pr_url=True, repo_config=None
    )
    assert result == "https://single.example.com/42"
