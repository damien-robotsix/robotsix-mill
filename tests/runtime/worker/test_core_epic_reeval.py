import asyncio
from types import SimpleNamespace

import pytest

from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import Worker, process_ticket
from robotsix_mill.stages import StageContext, registry
from robotsix_mill.stages.base import Stage


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


# --- epic re-evaluation helpers (extracted from _run_epic_reeval) ---


class _FakeWorkspace:
    """Stand-in for ``svc.workspace(obj)`` recording write calls."""

    def __init__(self, desc, calls):
        self._desc = desc
        self._calls = calls

    def read_description(self):
        return self._desc

    def write_description(self, note):
        self._calls.append(("write_description", note))
        return f"hash:{note}"


class _FakeEpicService:
    """Lightweight stand-in for ``TicketService`` used by the epic-reeval
    helpers; records every mutating call in ``calls``."""

    def __init__(self, children=None, descriptions=None, tickets=None, histories=None):
        self.children = children or []
        # obj.id -> description string returned by workspace().read_description
        self.descriptions = descriptions or {}
        # id -> ticket object returned by get(); id -> list of events by history()
        self.tickets = tickets or {}
        self.histories = histories or {}
        self.calls = []

    def list_children(self, epic_id):
        self.calls.append(("list_children", epic_id))
        return self.children

    def list_children_across_boards(self, epic_id):
        self.calls.append(("list_children_across_boards", epic_id))
        return self.children

    def get(self, ticket_id):
        return self.tickets.get(ticket_id)

    def history(self, ticket_id, offset=0, limit=None):
        return self.histories.get(ticket_id, [])

    def workspace(self, obj):
        return _FakeWorkspace(self.descriptions.get(obj.id, ""), self.calls)

    def transition(self, ticket_id, state, note=None):
        self.calls.append(("transition", ticket_id, state, note))

    def set_content_hash(self, ticket_id, content_hash):
        self.calls.append(("set_content_hash", ticket_id, content_hash))

    def set_depends_on(self, child_id, deps):
        self.calls.append(("set_depends_on", child_id, deps))

    def create(self, *, title, description, kind, parent_id):
        self.calls.append(("create", title, parent_id))
        from types import SimpleNamespace

        return SimpleNamespace(id=f"new-{len(self.calls)}", title=title)


def test_build_child_summaries_truncates_and_shapes():

    from robotsix_mill.runtime.worker import _build_child_summaries

    children = [
        SimpleNamespace(
            id="C1",
            title="Child one",
            state=SimpleNamespace(value="ready"),
            depends_on='["C0"]',
        ),
        SimpleNamespace(
            id="C2",
            title="Child two",
            state=SimpleNamespace(value="draft"),
            depends_on=None,
        ),
    ]
    svc = _FakeEpicService(
        children=children,
        descriptions={"C1": "x" * 600, "C2": "short"},
    )

    summaries = _build_child_summaries(svc, "E1")

    assert ("list_children_across_boards", "E1") in svc.calls
    assert [s["id"] for s in summaries] == ["C1", "C2"]
    assert summaries[0]["title"] == "Child one"
    assert summaries[0]["state"] == "ready"
    assert summaries[0]["depends_on"] == ["C0"]
    assert summaries[1]["depends_on"] == []
    # Long description truncated to 500 chars + suffix; short one untouched.
    assert summaries[0]["description"] == "x" * 500 + "\n...(truncated)"
    assert summaries[1]["description"] == "short"


def test_build_child_summaries_populates_distinct_delivery_labels():

    from robotsix_mill.runtime.worker import _build_child_summaries

    children = [
        SimpleNamespace(
            id="M", title="merged", state=SimpleNamespace(value="done"), depends_on=None
        ),
        SimpleNamespace(
            id="A", title="dedup", state=SimpleNamespace(value="done"), depends_on=None
        ),
        SimpleNamespace(
            id="U",
            title="unstarted",
            state=SimpleNamespace(value="draft"),
            depends_on=None,
        ),
    ]
    svc = _FakeEpicService(
        children=children,
        descriptions={"M": "m", "A": "a", "U": "u"},
        tickets={
            "M": _ns_ticket("M", State.DONE),
            "A": _ns_ticket("A", State.DONE),
            "U": _ns_ticket("U", State.DRAFT),
            "B": _ns_ticket("B", State.DRAFT),
        },
        histories={
            "M": [_ev(State.DONE, "merged: http://x/pr/1")],
            "A": [_ev(State.DONE, "duplicate of B: dupe")],
        },
    )

    summaries = {s["id"]: s["delivery"] for s in _build_child_summaries(svc, "E1")}

    assert summaries["M"] == "merged"
    assert summaries["U"] == "unstarted"
    assert "dedup" in summaries["A"].lower()
    # The three labels are distinguishable.
    assert len({summaries["M"], summaries["A"], summaries["U"]}) == 3


def test_handle_epic_decision_close():

    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService(descriptions={"E1": ""})
    result = EpicStatusResult(decision="close", note="done")

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert (
        "transition",
        "E1",
        State.EPIC_CLOSED,
        "[auto-closed] done",
    ) in svc.calls


def test_handle_epic_decision_keep_open_is_noop():

    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService()
    result = EpicStatusResult(decision="keep_open")

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert svc.calls == []


def test_handle_epic_decision_update_description():

    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService(descriptions={"E1": "old"})
    result = EpicStatusResult(decision="update_description", note="new body")

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert ("write_description", "new body") in svc.calls
    assert ("set_content_hash", "E1", "hash:new body") in svc.calls


def test_handle_epic_decision_update_deps_with_dep_updates():

    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService(descriptions={"E1": "old"})
    result = EpicStatusResult(
        decision="update_deps",
        dep_updates={"C1": ["C0"], "C2": None},
    )

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert ("set_depends_on", "C1", ["C0"]) in svc.calls
    # None entries normalize to an empty list.
    assert ("set_depends_on", "C2", []) in svc.calls
    # Empty note → no epic description rewrite.
    assert not any(c[0] == "write_description" for c in svc.calls)


def test_handle_epic_decision_close_with_new_children_downgrades():

    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService()
    result = EpicStatusResult(
        decision="close",
        note="done",
        new_children=[{"title": "follow-up", "body": "more work"}],
    )

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    # close + new_children downgrades to keep_open → no transition occurs.
    assert result.decision == "keep_open"
    assert not any(c[0] == "transition" for c in svc.calls)


def test_validate_epic_state_skips_blocked(monkeypatch):
    """_validate_epic_state returns None for a BLOCKED epic."""

    from robotsix_mill.core.states import State as S
    from robotsix_mill.runtime.worker import _validate_epic_state

    class _Settings:
        pass

    settings = _Settings()
    blocked_ticket = SimpleNamespace(id="E1", state=S.BLOCKED, board_id="b1")

    class _MockSvc:
        def get(self, ticket_id):
            return blocked_ticket

    mock_svc = _MockSvc()
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService",
        lambda *a, **kw: mock_svc,
    )
    result = _validate_epic_state(settings, "E1")
    assert result is None


def _ns_ticket(tid, state):

    return SimpleNamespace(id=tid, state=state)


def _ev(state, note=None):

    return SimpleNamespace(state=state, note=note)


def test_resolve_delivery_merged():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={"M": _ns_ticket("M", State.DONE)},
        histories={"M": [_ev(State.DONE, "merged: http://x/pr/1")]},
    )
    res = _resolve_delivery(svc, "M")
    assert res["delivered"] is True
    assert res["label"] == "merged"


def test_resolve_delivery_unstarted():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={"D": _ns_ticket("D", State.DRAFT)},
        histories={"D": []},
    )
    res = _resolve_delivery(svc, "D")
    assert res["delivered"] is False
    assert res["label"] == "unstarted"


def test_resolve_delivery_dedup_follows_chain_to_merged():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={
            "A": _ns_ticket("A", State.DONE),
            "B": _ns_ticket("B", State.DONE),
        },
        histories={
            "A": [_ev(State.DONE, "duplicate of B: same scope")],
            "B": [_ev(State.DONE, "merged: http://x/pr/2")],
        },
    )
    res = _resolve_delivery(svc, "A")
    assert res["delivered"] is True
    assert res["canonical"] == "B"
    assert "B" in res["label"]


def test_resolve_delivery_dedup_chain_not_delivered():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={
            "A": _ns_ticket("A", State.DONE),
            "B": _ns_ticket("B", State.DRAFT),
        },
        histories={
            "A": [_ev(State.DONE, "duplicate of B: same scope")],
            "B": [],
        },
    )
    res = _resolve_delivery(svc, "A")
    assert res["delivered"] is False
    assert res["canonical"] == "B"


def test_resolve_delivery_cyclic_dedup_does_not_raise():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={"A": _ns_ticket("A", State.DONE)},
        histories={"A": [_ev(State.DONE, "duplicate of A: self ref")]},
    )
    res = _resolve_delivery(svc, "A")
    assert res["delivered"] is False


def test_resolve_delivery_missing_ticket():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService()
    res = _resolve_delivery(svc, "gone")
    assert res["delivered"] is False


def _closure_svc(child_id, covering):
    """Build a fake service with a DRAFT child plus a covering sibling."""
    tickets = {child_id: _ns_ticket(child_id, State.DRAFT)}
    histories = {}
    for cid, (state, note) in covering.items():
        tickets[cid] = _ns_ticket(cid, state)
        histories[cid] = [_ev(state, note)] if note is not None else []
    return _FakeEpicService(tickets=tickets, histories=histories)


def test_reconcile_closes_draft_with_merged_covering_sibling():
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _closure_svc("C1", {"S1": (State.DONE, "merged: http://x/pr/9")})
    result = EpicStatusResult(decision="keep_open", child_closures={"C1": "S1"})

    _reconcile_child_changes(svc, "E1", result)

    transitions = [c for c in svc.calls if c[0] == "transition"]
    assert len(transitions) == 1
    _, tid, state, note = transitions[0]
    assert tid == "C1"
    assert state == State.CLOSED
    assert "S1" in note
    assert "Obsoleted by epic re-evaluation after sibling merge" not in note


@pytest.mark.parametrize(
    ("covering", "closures"),
    [
        # dedup-closed covering sibling whose canonical never merged
        ({"S1": (State.DONE, "duplicate of Z: dupe")}, {"C1": "S1"}),
        # unstarted covering sibling
        ({"S1": (State.DRAFT, None)}, {"C1": "S1"}),
        # self-reference
        ({}, {"C1": "C1"}),
        # unnamed covering sibling (legacy bare list)
        ({}, ["C1"]),
    ],
)
def test_reconcile_refuses_closure_without_merged_sibling(covering, closures):
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _closure_svc("C1", covering)
    result = EpicStatusResult(decision="keep_open", child_closures=closures)

    _reconcile_child_changes(svc, "E1", result)

    assert not any(c[0] == "transition" for c in svc.calls)


def test_reconcile_refuses_closure_missing_covering_ticket():
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    # Covering sibling id not present in tickets at all.
    svc = _FakeEpicService(tickets={"C1": _ns_ticket("C1", State.DRAFT)})
    result = EpicStatusResult(decision="keep_open", child_closures={"C1": "ghost"})

    _reconcile_child_changes(svc, "E1", result)

    assert not any(c[0] == "transition" for c in svc.calls)


def test_reconcile_incident_4564_unstarted_children_survive():
    """Reproduces epic 4564: A dedup-closed onto B; B/C/D unstarted; sibling
    E merged unrelated scope. A scope-blind closure of B/C/D (legacy list,
    no covering sibling) must NOT obsolete them."""
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _FakeEpicService(
        tickets={
            "B": _ns_ticket("B", State.DRAFT),
            "C": _ns_ticket("C", State.DRAFT),
            "D": _ns_ticket("D", State.DRAFT),
            "E": _ns_ticket("E", State.DONE),
        },
        histories={"E": [_ev(State.DONE, "merged: http://x/pr/unrelated")]},
    )
    # Scope-blind closure of the unstarted children with no covering sibling.
    result = EpicStatusResult(decision="keep_open", child_closures=["B", "C", "D"])

    _reconcile_child_changes(svc, "E1", result)

    # None of the unstarted Tier-1 children are obsoleted.
    assert not any(c[0] == "transition" for c in svc.calls)


def _aged_child(minutes_ago, title, state=State.DRAFT):
    """Child whose id carries a creation timestamp *minutes_ago* in the past."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    ts = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y%m%dT%H%M%S")
    slug = title.lower().replace(" ", "-")[:30]
    return SimpleNamespace(id=f"{ts}Z-{slug}-abcd", title=title, state=state)


def test_titles_similar_catches_epic_8d4a_duplicates():
    """The 2026-09-02 storm titles must match; unrelated siblings must not."""
    from robotsix_mill.runtime.worker.epic import _titles_similar

    assert _titles_similar(
        "Implement comprehensive regression tests for image handling and vision fallback",
        "Implement comprehensive regression test suite for vision fallback and error handling",
    )
    assert _titles_similar(
        "Document vision_model configuration and behavior",
        "Document vision_model configuration and caption fallback behavior",
    )
    assert _titles_similar(
        "Define and document rollback/disable strategy for vision route",
        "Implement rollback and disable strategy for vision route feature",
    )
    assert not _titles_similar(
        "Document vision_model configuration and behavior",
        "Define and document rollback/disable strategy for vision route",
    )
    assert not _titles_similar("", "anything")


def test_reconcile_new_children_skipped_while_recent_sibling_exists():
    """A sibling created within the window means a concurrent re-eval already
    filed follow-ups — re-proposing must be refused (epic 8d4a storm)."""
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _FakeEpicService(children=[_aged_child(2, "Fresh follow-up")])
    result = EpicStatusResult(
        decision="keep_open",
        new_children=[{"title": "Another follow-up", "body": "do the thing"}],
    )

    _reconcile_child_changes(svc, "E1", result)

    assert not any(c[0] == "create" for c in svc.calls)


def test_reconcile_new_children_near_duplicate_title_skipped():
    """A proposal near-duplicating a live sibling's title is refused; a
    genuinely new one is created."""
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _FakeEpicService(
        children=[_aged_child(60, "Document vision_model configuration and behavior")]
    )
    result = EpicStatusResult(
        decision="keep_open",
        new_children=[
            {
                "title": "Document vision_model configuration and caption fallback behavior",
                "body": "docs",
            },
            {"title": "Add prometheus metrics for caption cache", "body": "metrics"},
        ],
    )

    _reconcile_child_changes(svc, "E1", result)

    created = [c[1] for c in svc.calls if c[0] == "create"]
    assert created == ["Add prometheus metrics for caption cache"]


def test_reconcile_new_children_within_batch_twin_skipped():
    """Two near-identical proposals in ONE batch create only the first."""
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _FakeEpicService(children=[_aged_child(60, "Old unrelated child")])
    result = EpicStatusResult(
        decision="keep_open",
        new_children=[
            {"title": "Add regression tests for vision fallback", "body": "t"},
            {"title": "Add regression test suite for vision fallback", "body": "t"},
        ],
    )

    _reconcile_child_changes(svc, "E1", result)

    created = [c[1] for c in svc.calls if c[0] == "create"]
    assert created == ["Add regression tests for vision fallback"]


def test_stage_rank_covers_every_pipeline_state():
    """Every STAGE_FOR_STATE state must have an explicit _STAGE_RANK.

    A missing entry silently falls to _DEFAULT_STAGE_RANK (99) and is
    starved indefinitely on a busy board — every newly arriving draft or
    ready outranks it forever. Live case: REBASING was once absent, so
    blocked rebase tickets sat 75+ minutes with zero pickup while
    later-created drafts refined ahead of them.
    """
    from robotsix_mill.core.states import STAGE_FOR_STATE

    missing = [s for s in STAGE_FOR_STATE if s not in Worker._STAGE_RANK]
    assert not missing, (
        f"states with a pipeline stage but no explicit queue rank "
        f"(would be starved at default rank {Worker._DEFAULT_STAGE_RANK}): "
        f"{missing}"
    )


async def test_network_outage_parks_without_consuming_retry(ctx, service, monkeypatch):
    """A stage failing with a DNS-outage signature while the probe host
    is unresolvable is PARKED: next_retry_at set, retry budget never
    consumed, no BLOCKED transition — repeated failures (an outage far
    longer than stage_retry_max_attempts) keep parking instead of
    exhausting into a block."""
    import subprocess

    class DnsBoom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise subprocess.CalledProcessError(
                128,
                "git",
                stderr=(
                    "fatal: unable to access 'https://github.com/x/y/': "
                    "Could not resolve host: github.com"
                ),
            )

    monkeypatch.setitem(registry.STAGES, "refine", DnsBoom())
    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.network_available",
        lambda host, **kw: False,
    )
    t = service.create("x")
    for _ in range(ctx.settings.stage_retry_max_attempts + 2):
        await process_ticket(t.id, ctx)
        r = service.get(t.id)
        assert r.state is State.DRAFT, "outage must never block the ticket"
        assert r.retry_attempt == 1, "retry budget must not be consumed"
        assert r.next_retry_at is not None
        assert "network outage" in (r.last_transient_error or "")
        # Simulate the backoff elapsing so the next loop iteration
        # re-dispatches instead of short-circuiting on next_retry_at.
        service.set_retry_state(
            t.id,
            retry_attempt=r.retry_attempt,
            last_transient_error=r.last_transient_error,
            next_retry_at=None,
        )


async def test_network_error_with_connectivity_uses_bounded_retries(
    ctx, service, monkeypatch
):
    """The same DNS-flavored error WITHOUT a confirmed outage (probe
    host resolves) goes through the normal bounded transient retry —
    and blocks once attempts are exhausted."""
    import subprocess

    class DnsBoom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise subprocess.CalledProcessError(
                128,
                "git",
                stderr=(
                    "fatal: unable to access 'https://github.com/x/y/': "
                    "Could not resolve host: github.com"
                ),
            )

    monkeypatch.setitem(registry.STAGES, "refine", DnsBoom())
    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.network_available",
        lambda host, **kw: True,
    )
    t = service.create("x")
    for expected_attempt in range(1, ctx.settings.stage_retry_max_attempts + 1):
        await process_ticket(t.id, ctx)
        r = service.get(t.id)
        assert r.state is State.DRAFT
        assert r.retry_attempt == expected_attempt
        assert "network outage" not in (r.last_transient_error or "")
        service.set_retry_state(
            t.id,
            retry_attempt=r.retry_attempt,
            last_transient_error=r.last_transient_error,
            next_retry_at=None,
        )
    await process_ticket(t.id, ctx)
    r = service.get(t.id)
    assert r.state is State.BLOCKED, "exhausted retries must still block"


# -----------------------------------------------------------------------
# In-flight PR cap (max_inflight_prs)
# -----------------------------------------------------------------------


def test_max_inflight_prs_rejects_negative():
    """max_inflight_prs must reject negative values at construction time."""
    from robotsix_mill.config.repos import RepoConfig

    with pytest.raises(ValueError):  # pydantic ValidationError
        RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            max_inflight_prs=-1,
        )


def test_max_inflight_prs_accepts_zero():
    """max_inflight_prs=0 is valid (disables the cap)."""
    from robotsix_mill.config.repos import RepoConfig

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_inflight_prs=0,
    )
    assert rc.max_inflight_prs == 0


def test_max_inflight_prs_defaults_to_3():
    """Omitting max_inflight_prs defaults to 3."""
    from robotsix_mill.config.repos import RepoConfig

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    assert rc.max_inflight_prs == 3


def test_count_inflight_prs_counts_only_in_flight_states(service):
    """_count_inflight_prs returns the count of tickets in _IN_FLIGHT_PR_STATES."""
    from robotsix_mill.runtime.worker.core import _count_inflight_prs

    # Initially empty.
    assert _count_inflight_prs(service) == 0

    # Create tickets in various states.
    t1 = service.create("ready ticket")
    t2 = service.create("deliverable pr")

    # Transition t2 to DELIVERABLE (in-flight).
    for st in (State.READY, State.DELIVERABLE):
        service.transition(t2.id, st)
    assert service.get(t2.id).state is State.DELIVERABLE
    assert _count_inflight_prs(service) == 1

    # t1 is DRAFT (not in-flight) — shouldn't count.
    assert service.get(t1.id).state is State.DRAFT
    assert _count_inflight_prs(service) == 1

    # Move t1 to IMPLEMENT_COMPLETE → now 2 in-flight.
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t1.id, st)
    assert service.get(t1.id).state is State.IMPLEMENT_COMPLETE
    assert _count_inflight_prs(service) == 2


def test_count_inflight_prs_excludes_non_in_flight_states(service):
    """HUMAN_MR_APPROVAL, BLOCKED, and DRAFT/READY must NOT count toward the cap."""
    from robotsix_mill.runtime.worker.core import _count_inflight_prs

    # Create tickets and move them to various non-in-flight states.
    t_hmr = service.create("human mr approval")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(t_hmr.id, st)

    t_blocked = service.create("blocked ticket")
    for st in (State.READY, State.DELIVERABLE):
        service.transition(t_blocked.id, st)
    # Move to BLOCKED via direct state set (the worker does this).
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    with _db.session(service.settings, service.board_id) as s:
        row = s.get(_Ticket, t_blocked.id)
        row.state = State.BLOCKED
        s.add(row)
        s.commit()

    assert service.get(t_hmr.id).state is State.HUMAN_MR_APPROVAL
    assert service.get(t_blocked.id).state is State.BLOCKED
    assert _count_inflight_prs(service) == 0


async def test_cap_blocks_ready_when_at_limit(ctx, service, monkeypatch):
    """With max_inflight_prs=1 and one DELIVERABLE ticket, a popped READY
    ticket must be re-enqueued rather than dispatched to implement."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Create one in-flight PR ticket (DELIVERABLE).
    inflight = service.create("in-flight pr")
    for st in (State.READY, State.DELIVERABLE):
        service.transition(inflight.id, st)
    assert service.get(inflight.id).state is State.DELIVERABLE
    assert _count_inflight_prs(service) == 1

    # A READY ticket — should be blocked by the cap.
    ready_ticket = service.create("ready to implement")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, p_ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id not in invoked, "READY ticket must NOT be dispatched at cap"


async def test_cap_blocks_draft_when_at_limit(ctx, service, monkeypatch):
    """With max_inflight_prs=1 and one DELIVERABLE ticket, a popped DRAFT
    ticket must be re-enqueued rather than dispatched to refine."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Create one in-flight PR ticket (DELIVERABLE).
    inflight = service.create("in-flight pr")
    for st in (State.READY, State.DELIVERABLE):
        service.transition(inflight.id, st)
    assert service.get(inflight.id).state is State.DELIVERABLE
    assert _count_inflight_prs(service) == 1

    # A DRAFT ticket — should be blocked by the cap.
    draft_ticket = service.create("draft to refine")

    w = Worker(ctx)
    w.enqueue(draft_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, p_ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert draft_ticket.id not in invoked, "DRAFT ticket must NOT be dispatched at cap"


async def test_cap_allows_ready_when_below_limit(ctx, service, monkeypatch):
    """With max_inflight_prs=3 and only 2 in-flight tickets, a READY
    ticket proceeds normally."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=3,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Two in-flight.
    for i in range(2):
        t = service.create(f"in-flight-{i}")
        for st in (State.READY, State.DELIVERABLE):
            service.transition(t.id, st)

    assert _count_inflight_prs(service) == 2

    ready_ticket = service.create("ready below cap")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id in invoked, (
        "READY ticket should have been dispatched — below cap"
    )


async def test_cap_disabled_when_zero(ctx, service, monkeypatch):
    """max_inflight_prs=0 disables the cap entirely — all READY tickets
    are dispatched regardless of in-flight count."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=0,  # disabled
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Several in-flight — more than the default of 3.
    for i in range(5):
        t = service.create(f"in-flight-{i}")
        for st in (State.READY, State.DELIVERABLE):
            service.transition(t.id, st)

    assert _count_inflight_prs(service) == 5

    ready_ticket = service.create("ready when cap disabled")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id in invoked, (
        "READY ticket should be dispatched when max_inflight_prs=0"
    )


async def test_merge_pipeline_always_processed_at_cap(ctx, service, monkeypatch):
    """At cap, a merge-pipeline ticket (IMPLEMENT_COMPLETE) is processed
    normally — the cap only gates READY/DRAFT."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # One in-flight — at cap.
    t1 = service.create("in-flight")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t1.id, st)
    assert service.get(t1.id).state is State.IMPLEMENT_COMPLETE
    assert _count_inflight_prs(service) == 1

    # Another merge-pipeline ticket (also IMPLEMENT_COMPLETE) — should
    # still be processed regardless of cap.
    t2 = service.create("another merge")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t2.id, st)
    assert service.get(t2.id).state is State.IMPLEMENT_COMPLETE

    w = Worker(ctx)
    w.enqueue(t2.id)

    invoked = []

    async def fake_process_ticket(ticket_id, ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert t2.id in invoked, (
        "IMPLEMENT_COMPLETE (merge-pipeline) ticket must be processed at cap"
    )


async def test_cap_excludes_human_mr_approval_from_count(ctx, service, monkeypatch):
    """HUMAN_MR_APPROVAL tickets do NOT count toward the in-flight cap.

    A repo at cap=1 with one HUMAN_MR_APPROVAL ticket (and zero actual
    in-flight PRs) should still dispatch new READY work.
    """
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Create one HUMAN_MR_APPROVAL ticket — excluded from in-flight count.
    parked = service.create("human approval pending")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.WAITING_AUTO_MERGE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(parked.id, st)
    assert service.get(parked.id).state is State.HUMAN_MR_APPROVAL
    # HUMAN_MR_APPROVAL is excluded → count is 0 even with cap=1.
    assert _count_inflight_prs(service) == 0

    # A READY ticket — should proceed because the cap isn't actually at limit.
    ready_ticket = service.create("ready despite parked approval")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, p_ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id in invoked, (
        "READY ticket should be dispatched — HUMAN_MR_APPROVAL does not count"
    )
