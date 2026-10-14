"""Tests for the budget-cap auto-split fallback.

The fallback splits a ticket that repeatedly exhausts the coordinator
tool-call budget into independently-shippable epic children, instead of
looping resume→cap→block forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import budget_cap_autosplit as acs

# ---------------------------------------------------------------------------
#  Fakes
# ---------------------------------------------------------------------------


def _ev(state: State, note: str, minute: int):
    return SimpleNamespace(
        state=state,
        note=note,
        at=datetime(2026, 10, 14, 0, minute, 0, tzinfo=UTC),
    )


class _FakeService:
    def __init__(self, ticket, events=None, children=None):
        self._ticket = ticket
        self._events = events or []
        self._children = list(children or [])
        self.created: list[SimpleNamespace] = []
        self.depends_on_calls: list[tuple[str, list[str]]] = []
        self.promoted = False
        self.notes: list[str] = []
        self._counter = 0

    def history(self, ticket_id, order="asc"):
        evs = list(self._events)
        return evs if order == "asc" else list(reversed(evs))

    def get(self, ticket_id):
        return self._ticket

    def list_children(self, ticket_id):
        return list(self._children)

    def promote_to_epic(self, ticket_id):
        self.promoted = True
        self._ticket.kind = TicketKind.EPIC

    def create(self, *, title, description, kind=TicketKind.TASK, parent_id=None):
        self._counter += 1
        child = SimpleNamespace(
            id=f"child-{self._counter}",
            title=title,
            description=description,
            kind=kind,
            parent_id=parent_id,
        )
        self.created.append(child)
        return child

    def set_depends_on(self, child_id, deps):
        self.depends_on_calls.append((child_id, list(deps)))

    def add_history_note(self, ticket_id, note):
        self.notes.append(note)


def _ticket(kind=TicketKind.TASK):
    return SimpleNamespace(id="parent-1", title="Big ticket", kind=kind)


def _ctx(service, threshold=3):
    settings = SimpleNamespace(coordinator_budget_cap_autosplit_threshold=threshold)
    return SimpleNamespace(settings=settings, service=service)


def _cap_events(n):
    return [
        _ev(State.BLOCKED, "agent budget cap — resumable (move to READY): x", i)
        for i in range(n)
    ]


class _Breakdown:
    def __init__(self, titles, bodies, epic_body=""):
        self.child_titles = titles
        self.child_bodies = bodies
        self.epic_body = epic_body


@pytest.fixture
def patched_breakdown(monkeypatch):
    """Patch epic-breakdown + progress gate for orchestration tests."""
    import robotsix_mill.agents.epic_breakdown as eb

    calls = {}

    def fake_run(*, settings, epic_title, epic_description, **kw):
        calls["epic_title"] = epic_title
        calls["epic_description"] = epic_description
        return _Breakdown(["Child A", "Child B"], ["body a", "body b"])

    monkeypatch.setattr(eb, "run_epic_breakdown_agent", fake_run)
    monkeypatch.setattr(eb, "plan_child_dependencies", lambda created, **kw: {})
    monkeypatch.setattr(acs, "_branch_has_progress", lambda *a, **k: True)
    return calls


# ---------------------------------------------------------------------------
#  count_budget_cap_pauses
# ---------------------------------------------------------------------------


def test_count_only_budget_cap_blocked_events():
    events = [
        _ev(State.BLOCKED, "agent budget cap — resumable (move to READY): foo", 1),
        _ev(State.BLOCKED, "clone missing — resumable", 2),  # not a budget cap
        _ev(State.READY, "agent budget cap — resumable", 3),  # wrong state
        _ev(State.BLOCKED, "agent budget cap — resumable (move to READY): bar", 4),
    ]
    svc = _FakeService(_ticket(), events=events)
    stamps = acs.count_budget_cap_pauses(_ctx(svc), "parent-1")
    assert len(stamps) == 2


# ---------------------------------------------------------------------------
#  maybe_autosplit_on_budget_cap — threshold / disable
# ---------------------------------------------------------------------------


def test_disabled_when_threshold_zero(patched_breakdown):
    svc = _FakeService(_ticket(), events=_cap_events(9))
    assert (
        acs.maybe_autosplit_on_budget_cap(
            _ctx(svc, threshold=0), svc._ticket, Path("."), "b", "main", "spec"
        )
        is None
    )
    assert not svc.promoted


def test_below_threshold_does_not_split(patched_breakdown):
    # threshold 3, one prior pause + this cap = 2 < 3
    svc = _FakeService(_ticket(), events=_cap_events(1))
    out = acs.maybe_autosplit_on_budget_cap(
        _ctx(svc, threshold=3), svc._ticket, Path("."), "b", "main", "spec"
    )
    assert out is None
    assert not svc.promoted


def test_splits_at_threshold(patched_breakdown):
    # two prior pauses + this cap = 3 == threshold
    svc = _FakeService(_ticket(), events=_cap_events(2))
    out = acs.maybe_autosplit_on_budget_cap(
        _ctx(svc, threshold=3), svc._ticket, Path("."), "b", "main", "the spec text"
    )
    assert out is not None
    assert out.next_state is State.EPIC_OPEN
    assert svc.promoted
    assert len(svc.created) == 2
    assert all(c.parent_id == "parent-1" for c in svc.created)
    assert acs.AUTOSPLIT_NOTE_MARKER in out.note
    # epic breakdown fed the ticket's spec
    assert patched_breakdown["epic_description"] == "the spec text"


# ---------------------------------------------------------------------------
#  idempotency
# ---------------------------------------------------------------------------


def test_already_epic_not_resplit(patched_breakdown):
    svc = _FakeService(_ticket(kind=TicketKind.EPIC), events=_cap_events(5))
    out = acs.maybe_autosplit_on_budget_cap(
        _ctx(svc, threshold=3), svc._ticket, Path("."), "b", "main", "spec"
    )
    assert out is None
    assert not svc.created


def test_existing_children_not_resplit(patched_breakdown):
    svc = _FakeService(
        _ticket(), events=_cap_events(5), children=[SimpleNamespace(id="c0")]
    )
    out = acs.maybe_autosplit_on_budget_cap(
        _ctx(svc, threshold=3), svc._ticket, Path("."), "b", "main", "spec"
    )
    assert out is None
    assert not svc.created


# ---------------------------------------------------------------------------
#  progress gate — no-progress tickets stay capped, not split
# ---------------------------------------------------------------------------


def test_no_progress_not_split(monkeypatch):
    import robotsix_mill.agents.epic_breakdown as eb

    monkeypatch.setattr(
        eb,
        "run_epic_breakdown_agent",
        lambda **kw: pytest.fail("breakdown must not run for a no-progress ticket"),
    )
    monkeypatch.setattr(acs, "_branch_has_progress", lambda *a, **k: False)
    svc = _FakeService(_ticket(), events=_cap_events(5))
    out = acs.maybe_autosplit_on_budget_cap(
        _ctx(svc, threshold=3), svc._ticket, Path("."), "b", "main", "spec"
    )
    assert out is None
    assert not svc.promoted


# ---------------------------------------------------------------------------
#  breakdown failure / empty — fall back to block
# ---------------------------------------------------------------------------


def test_breakdown_exception_falls_back_to_block(monkeypatch):
    import robotsix_mill.agents.epic_breakdown as eb

    def boom(**kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(eb, "run_epic_breakdown_agent", boom)
    monkeypatch.setattr(acs, "_branch_has_progress", lambda *a, **k: True)
    svc = _FakeService(_ticket(), events=_cap_events(5))
    out = acs.maybe_autosplit_on_budget_cap(
        _ctx(svc, threshold=3), svc._ticket, Path("."), "b", "main", "spec"
    )
    assert out is None
    assert not svc.promoted  # parent untouched, still resumable


def test_breakdown_no_children_falls_back_to_block(monkeypatch):
    import robotsix_mill.agents.epic_breakdown as eb

    monkeypatch.setattr(eb, "run_epic_breakdown_agent", lambda **kw: _Breakdown([], []))
    monkeypatch.setattr(eb, "plan_child_dependencies", lambda created, **kw: {})
    monkeypatch.setattr(acs, "_branch_has_progress", lambda *a, **k: True)
    svc = _FakeService(_ticket(), events=_cap_events(5))
    out = acs.maybe_autosplit_on_budget_cap(
        _ctx(svc, threshold=3), svc._ticket, Path("."), "b", "main", "spec"
    )
    assert out is None
    assert not svc.promoted


# ---------------------------------------------------------------------------
#  _branch_has_progress — real git plumbing
# ---------------------------------------------------------------------------


def test_branch_has_progress_true_on_dirty_tree(tmp_path):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("hello")
    assert acs._branch_has_progress(repo, "b", "main") is True


def test_branch_has_progress_false_on_clean_no_commits(tmp_path):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.co"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    # HEAD == main, clean tree, nothing ahead → no progress
    assert acs._branch_has_progress(repo, "main", "main") is False
