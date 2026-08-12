"""Epic-related tests extracted from test_service.py.

These tests cover epic creation, child linkage, archived-ticket purge,
mark_done (merge verification, changelog fragment gate, citation
verification), and related concerns.

Shared helpers (_close_ticket, _answer_ticket, _close_epic,
_terminal_count, _comment_count, _get_comment) are imported from
test_service.py because they are also used by other test sections.
"""

import pytest

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TransitionError
from robotsix_mill.core.states import State, can_transition
from tests.core.test_service import (
    _answer_ticket,
    _close_epic,
    _close_ticket,
    _terminal_count,
)

# ---------------------------------------------------------------------------
# Epic tests
# ---------------------------------------------------------------------------


def test_create_epic(service):
    """Creating with kind='epic' sets state to EPIC_OPEN."""
    t = service.create("My Epic", "Big picture", kind=TicketKind.EPIC)
    assert t.state == State.EPIC_OPEN
    assert t.kind == TicketKind.EPIC


def test_create_child_with_parent_id(service):
    """Creating a child with parent_id links it to the epic."""
    epic = service.create("Epic", "Overview", kind=TicketKind.EPIC)
    child = service.create("Child", "Detail", kind=TicketKind.TASK, parent_id=epic.id)
    assert child.parent_id == epic.id
    # Verify persisted
    reloaded = service.get(child.id)
    assert reloaded.parent_id == epic.id


def test_create_child_nonexistent_parent(service):
    """parent_id pointing to a missing ticket raises ValueError."""
    with pytest.raises(ValueError, match="does not exist"):
        service.create("Orphan", "desc", parent_id="nonexistent-id")


def test_get_epic_context_returns_description(service):
    """get_epic_context returns the parent epic description wrapped in tags."""
    epic = service.create("Epic", "Big picture description", kind=TicketKind.EPIC)
    child = service.create("Child", "detail", kind=TicketKind.TASK, parent_id=epic.id)
    ctx = service.get_epic_context(child)
    assert (
        ctx == "````epic-context\nBig picture description\n````\n<!-- /epic-context -->"
    )


def test_get_epic_context_no_parent(service):
    """get_epic_context returns '' for a ticket without a parent."""
    t = service.create("Standalone")
    assert service.get_epic_context(t) == ""


def test_get_epic_context_parent_not_epic(service):
    """get_epic_context returns '' when parent is not an epic."""
    parent = service.create("Regular parent", kind=TicketKind.TASK)
    child = service.create("Child", "desc", kind=TicketKind.TASK, parent_id=parent.id)
    assert service.get_epic_context(child) == ""


def test_list_children(service):
    """list_children returns all tickets with the given parent_id."""
    epic = service.create("Epic", "Overview", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", kind=TicketKind.TASK, parent_id=epic.id)
    c2 = service.create("Child 2", kind=TicketKind.TASK, parent_id=epic.id)
    c3 = service.create("Child 3", kind=TicketKind.TASK, parent_id=epic.id)
    children = service.list_children(epic.id)
    assert len(children) == 3
    child_ids = {c.id for c in children}
    assert child_ids == {c1.id, c2.id, c3.id}


# --- archived-ticket purge ---------------------------------------------


class TestArchivedPurge:
    """Tests for insertion-driven purge of terminal (archived) tickets."""

    def test_no_op_when_under_cap(self, service, settings):
        """No tickets are deleted when the terminal count is under the cap."""
        settings.max_archived_tickets = 10
        for i in range(5):
            t = service.create(f"task {i}")
            _close_ticket(service, t)
        assert _terminal_count(service) == 5

    def test_deletes_oldest_on_cap_exceeded(self, service, settings):
        """When closing ticket N+1 exceeds the cap, the oldest terminal
        ticket is deleted."""
        settings.max_archived_tickets = 3
        tickets = []
        for i in range(4):
            t = service.create(f"task {i}")
            _close_ticket(service, t)
            tickets.append(t)

        # The oldest (tickets[0]) should have been purged.
        assert service.get(tickets[0].id) is None
        # The other three should still exist.
        for t in tickets[1:]:
            assert service.get(t.id) is not None
        assert _terminal_count(service) == 3

    def test_answered_triggers_purge(self, service, settings):
        """Answering an inquiry (ANSWERED) also triggers the purge."""
        settings.max_archived_tickets = 2
        inquiries = []
        for i in range(3):
            t = service.create(f"inquiry {i}", kind=TicketKind.INQUIRY)
            _answer_ticket(service, t)
            inquiries.append(t)

        # Oldest should be purged.
        assert service.get(inquiries[0].id) is None
        assert service.get(inquiries[1].id) is not None
        assert service.get(inquiries[2].id) is not None
        assert _terminal_count(service) == 2

    def test_epic_closed_triggers_purge(self, service, settings):
        """Closing an epic (EPIC_CLOSED) also triggers the purge."""
        settings.max_archived_tickets = 2
        epics = []
        for i in range(3):
            t = service.create(f"epic {i}", kind=TicketKind.EPIC)
            _close_epic(service, t)
            epics.append(t)

        assert service.get(epics[0].id) is None
        assert service.get(epics[1].id) is not None
        assert service.get(epics[2].id) is not None
        assert _terminal_count(service) == 2

    def test_skip_parent_of_active_child(self, service, settings):
        """A terminal ticket that is the parent of an active child is
        skipped during purge; the next-oldest eligible ticket is
        deleted instead."""
        settings.max_archived_tickets = 2

        # Create 3 terminal tickets.
        t1 = service.create("oldest task")
        _close_ticket(service, t1)

        t2 = service.create("parent task")
        _close_ticket(service, t2)

        t3 = service.create("youngest task")
        _close_ticket(service, t3)

        # t2 has an active (non-terminal) child.
        child = service.create("active child", parent_id=t2.id)
        assert child.state == State.DRAFT  # active

        # Now trigger purge by closing a 4th ticket.
        t4 = service.create("overflow task")
        _close_ticket(service, t4)

        # t2 (parent of active child) should survive.
        assert service.get(t2.id) is not None
        # t1 (oldest, no active children) should be purged.
        assert service.get(t1.id) is None
        # t3 (next oldest after t1) also has no children, so it is
        # purged to bring the count down to the cap of 2.
        assert service.get(t3.id) is None
        # t4 (just closed) survives.
        assert service.get(t4.id) is not None
        # Terminal count is 2: t2 (skipped parent) + t4.
        assert _terminal_count(service) == 2

    def test_max_archived_zero_disables_purge(self, service, settings):
        """Setting max_archived_tickets = 0 disables purging entirely."""
        settings.max_archived_tickets = 0
        for i in range(50):
            t = service.create(f"task {i}")
            _close_ticket(service, t)
        assert _terminal_count(service) == 50


# ---------------------------------------------------------------------------
# delete cascades to comments
# ---------------------------------------------------------------------------


def test_delete_cascades_to_comments(service):
    """Deleting a ticket also removes its Comment rows."""
    t = service.create("target")
    c = service.add_comment(t.id, "will be cascade-deleted", author="test")

    from robotsix_mill.core import db
    from robotsix_mill.core.models import Comment

    # Confirm it exists before delete.
    with db.session(service.settings, service.board_id) as s:
        assert s.get(Comment, c.id) is not None

    service.delete(t.id)

    # After ticket delete, the comment should be gone.
    with db.session(service.settings, service.board_id) as s:
        assert s.get(Comment, c.id) is None


# ---------------------------------------------------------------------------
# _all_descendants cycle-safety
# ---------------------------------------------------------------------------


def test_all_descendants_is_cycle_safe(service):
    """Directly insert rows where A → B → A (circular parent_id).
    _all_descendants('A') returns [B] without infinite looping."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket

    with db.session(service.settings, service.board_id) as s:
        ta = Ticket(id="cyc-A", title="A", kind=TicketKind.TASK, workspace_path="")
        tb = Ticket(
            id="cyc-B",
            title="B",
            kind=TicketKind.TASK,
            parent_id="cyc-A",
            workspace_path="",
        )
        s.add_all([ta, tb])
        s.commit()

        # Create the cycle: update A's parent_id to point to B.
        ta.parent_id = "cyc-B"
        s.add(ta)
        s.commit()

    result = service._all_descendants("cyc-A")
    assert len(result) == 1
    assert result[0].id == "cyc-B"


def test_transition_no_proposals_clean_transition(service):
    """A ticket transitions cleanly to a terminal state (no error)."""
    t = service.create("No proposals ticket")
    # Should not raise.
    service.transition(t.id, State.DONE)
    service.transition(t.id, State.CLOSED)
    assert service.get(t.id).state is State.CLOSED


# -- mark_done ----------------------------------------------------------


def test_mark_done_from_draft(service):
    """mark_done transitions a DRAFT ticket to DONE and records a
    TicketEvent."""
    t = service.create("mark me done")
    comment, ticket = service.mark_done(t.id)
    assert comment is None
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert hist[-1].state is State.DONE
    assert hist[-1].note == "mark done"


def test_mark_done_from_blocked(service):
    """mark_done transitions a BLOCKED ticket to DONE with a force‑close
    marker in the note."""
    t = service.create("blocked mark done")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    comment, ticket = service.mark_done(t.id)
    assert comment is not None
    assert "[force-closed from blocked] operator mark-done" in comment.body
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert "[force-closed from blocked]" in hist[-1].note


def test_mark_done_from_blocked_with_caller_note(service):
    """When a caller supplies a note on a BLOCKED ticket the force‑close
    marker is prepended and the caller text is preserved."""
    t = service.create("blocked with reason")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    comment, ticket = service.mark_done(t.id, note="PR #123 already merged")
    assert comment is not None
    assert comment.body == "[force-closed from blocked] PR #123 already merged"
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert "[force-closed from blocked] PR #123 already merged" in hist[-1].note


def _make_repo_with_unmerged_branch(service, t, branch: str) -> None:
    """Create a workspace clone whose *branch* carries a commit that is
    NOT on origin/main, so ``verify_merge_before_done`` would raise."""
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)

    def _g(*args):
        _sp.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    _g("init")
    _g("config", "user.email", "test@example.com")
    _g("config", "user.name", "Test")
    (repo / "file.txt").write_text("base")
    _g("add", ".")
    _g("commit", "-m", "initial")
    # origin/main pinned at the base commit.
    _g("branch", "origin/main")
    # A feature branch with an extra, un-merged commit.
    _g("checkout", "-b", branch)
    (repo / "file.txt").write_text("feature change")
    _g("commit", "-am", "unmerged work")
    service.set_branch(t.id, branch)


def test_mark_done_from_blocked_bypasses_merge_verify(service):
    """Escape hatch: an operator can force-close a stuck BLOCKED ticket
    whose branch was never merged. The merge-verification gate (which
    would otherwise 409) is skipped for the deliberate override."""
    t = service.create("blocked no-op loop")
    branch = f"{service.settings.branch_prefix}{t.id}"
    _make_repo_with_unmerged_branch(service, t, branch)
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="PR closed without merge — resumable")

    comment, ticket = service.mark_done(t.id, note="already satisfied on main")
    assert ticket.state is State.DONE
    assert comment is not None
    assert "[force-closed from blocked] already satisfied on main" in comment.body


def test_mark_done_from_rebasing_bypasses_merge_verify(service):
    """Escape hatch also works from REBASING (a ticket wedged in the
    rebase agent), whose branch is conflicting/un-merged."""
    t = service.create("rebasing wedge")
    branch = f"{service.settings.branch_prefix}{t.id}"
    _make_repo_with_unmerged_branch(service, t, branch)
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.REBASING, note="conflicting")

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE
    assert comment is not None
    assert "[force-closed from rebasing] operator mark-done" in comment.body


def test_mark_done_still_verifies_merge_from_normal_state(service):
    """The escape-hatch bypass is scoped to BLOCKED/REBASING only: a
    normal (non-stuck) state with an un-merged branch still refuses
    mark-done so an operator can't prematurely close an open PR."""
    t = service.create("open pr premature close")
    branch = f"{service.settings.branch_prefix}{t.id}"
    _make_repo_with_unmerged_branch(service, t, branch)
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.HUMAN_MR_APPROVAL)

    with pytest.raises(TransitionError, match="not been merged"):
        service.mark_done(t.id)


def test_mark_done_with_note_creates_comment(service):
    """A non-empty note creates a Comment alongside the event."""
    t = service.create("with note")
    comment, ticket = service.mark_done(t.id, note="done manually")
    assert comment is not None
    assert comment.body == "done manually"
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert hist[-1].note == "mark done: done manually"


def test_mark_done_rejects_terminal(service):
    """mark_done raises TransitionError for CLOSED (terminal)."""
    t = service.create("closed ticket")
    # walk to CLOSED
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.HUMAN_MR_APPROVAL)
    service.transition(t.id, State.DONE)
    service.transition(t.id, State.CLOSED)
    with pytest.raises(TransitionError):
        service.mark_done(t.id)


def test_mark_done_rejects_already_done(service):
    """mark_done raises TransitionError for DONE tickets."""
    t = service.create("already done")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.HUMAN_MR_APPROVAL)
    service.transition(t.id, State.DONE)
    with pytest.raises(TransitionError):
        service.mark_done(t.id)


def test_mark_done_rejects_epic_open(service):
    """mark_done raises TransitionError for EPIC_OPEN tickets."""
    t = service.create("epic open", kind=TicketKind.EPIC)
    assert t.state is State.EPIC_OPEN
    with pytest.raises(TransitionError):
        service.mark_done(t.id)


def test_transition_epic_open_to_epic_closed(service):
    """transition() allows EPIC_OPEN → EPIC_CLOSED."""
    t = service.create("abandon me", kind=TicketKind.EPIC)
    assert t.state is State.EPIC_OPEN
    updated = service.transition(t.id, State.EPIC_CLOSED, note="abandoned")
    assert updated.state is State.EPIC_CLOSED


def test_mark_done_auto_closes_open_ask_user_threads(service):
    """mark_done closes open [ASK_USER] threads and records it in the note."""
    t = service.create("Force-closing with open question")
    service.add_comment(t.id, "[ASK_USER]\n\nShould we proceed?", author="implement")

    comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE
    assert comment is not None
    assert "[force-closed with 1 open [ASK_USER] thread" in comment.body

    # The [ASK_USER] thread must now be closed.
    comments = service.list_comments(t.id)
    ask_comments = [cm for cm in comments if cm.body.startswith("[ASK_USER]")]
    assert len(ask_comments) == 1
    assert ask_comments[0].closed_at is not None


def test_mark_done_no_ask_user_threads_clean_note(service):
    """When there are no open [ASK_USER] threads, the note is unchanged."""
    t = service.create("Clean force-close")
    comment, ticket = service.mark_done(t.id, note="no longer needed")
    assert ticket.state is State.DONE
    assert comment is not None
    assert comment.body == "no longer needed"
    assert "[force-closed" not in comment.body


# -- mark_done citation verification -----------------------------------


def test_mark_done_empty_note_still_works(service):
    """Empty/default note passes through cleanly — no warnings appended."""
    t = service.create("empty note mark done")
    comment, ticket = service.mark_done(t.id)
    assert comment is None
    assert ticket.state is State.DONE
    hist = service.history(t.id)
    assert hist[-1].note == "mark done"


def test_mark_done_with_nonexistent_pr_appends_warning(service, tmp_path):
    """PR #1562 not on origin/main → ⚠️ appended to comment body."""
    t = service.create("nonexistent pr test")
    # Simulate a workspace clone with a real git repo so git commands
    # can run (origin/main must exist with at least one commit).
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("hello")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True,
        text=True,
    )
    # Create origin/main ref pointing at the initial commit.
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    comment, ticket = service.mark_done(t.id, note="Root cause fixed in PR #1562")
    assert comment is not None
    assert "⚠️" in comment.body
    assert "PR #1562" in comment.body
    assert "not found on origin/main" in comment.body
    assert ticket.state is State.DONE


def test_mark_done_with_existing_pr_stores_cleanly(service, tmp_path):
    """PR #42 found in origin/main commit message → no warning."""
    t = service.create("existing pr test")
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("hello")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "Merge PR #42 into main"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    comment, ticket = service.mark_done(t.id, note="Fixed by PR #42")
    assert ticket.state is State.DONE
    if comment is not None:
        assert "⚠️" not in comment.body


def test_mark_done_with_unverifiable_commit_sha_warns(service, tmp_path):
    """Bogus SHA → ⚠️ appended."""
    t = service.create("bogus sha test")
    import subprocess as _sp

    ws = service.workspace(t)
    repo = ws.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("hello")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    comment, ticket = service.mark_done(
        t.id, note="Cherry-picked abcdef1234567890abcdef1234567890abcdef12"
    )
    assert comment is not None
    assert "⚠️" in comment.body
    assert "abcdef1234567890abcdef1234567890abcdef12" in comment.body
    assert "not found on origin/main" in comment.body
    assert ticket.state is State.DONE


def test_mark_done_no_repo_clone_no_crash(service):
    """Missing repo_dir → graceful no-op (note stored verbatim)."""
    t = service.create("no clone test")
    ws = service.workspace(t)
    # Ensure no repo clone exists.
    if ws.repo_dir.exists():
        import shutil

        shutil.rmtree(ws.repo_dir)

    comment, ticket = service.mark_done(t.id, note="Fixed in PR #9999")
    assert ticket.state is State.DONE
    if comment is not None:
        assert comment.body == "Fixed in PR #9999"


# -- Changelog duplicate fragment gate ---------------------------------


def _setup_repo_with_towncrier(repo_dir, fragment_dir_name="changes"):
    """Create a git repo with a pyproject.toml declaring towncrier config."""
    import subprocess as _sp

    repo_dir.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo_dir), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )

    pp = repo_dir / "pyproject.toml"
    pp.write_text(f'[tool.towncrier]\ndirectory = "{fragment_dir_name}"\n')
    _sp.run(
        ["git", "-C", str(repo_dir), "add", "pyproject.toml"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "init with towncrier"],
        capture_output=True,
        text=True,
    )


def _add_fragment(repo_dir, fragment_dir_name, filename, content="fragment content"):
    """Create a fragment file, stage and commit it."""
    import subprocess as _sp

    frag_dir = repo_dir / fragment_dir_name
    frag_dir.mkdir(parents=True, exist_ok=True)
    (frag_dir / filename).write_text(content)
    _sp.run(
        ["git", "-C", str(repo_dir), "add", f"{fragment_dir_name}/{filename}"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "commit", "-m", f"add {filename}"],
        capture_output=True,
        text=True,
    )


def _setup_repo_with_branch(repo_dir, ticket_id, branch_prefix="mill/"):
    """Create a git repo with an initial commit on origin/main and a
    feature branch <branch_prefix><ticket_id>.

    Returns (repo_dir, branch_name).
    """
    import subprocess as _sp

    repo_dir.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo_dir), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo_dir / "README.md").write_text("initial")
    _sp.run(["git", "-C", str(repo_dir), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "initial commit"],
        capture_output=True,
        text=True,
    )
    # Create origin/main as a local ref (simulates remote tracking branch).
    _sp.run(
        ["git", "-C", str(repo_dir), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )
    branch = f"{branch_prefix}{ticket_id}"
    _sp.run(
        ["git", "-C", str(repo_dir), "checkout", "-b", branch],
        capture_output=True,
        text=True,
    )
    return branch


def _advance_origin_main(repo_dir):
    """Point origin/main to the current HEAD."""
    import subprocess as _sp

    _sp.run(
        ["git", "-C", str(repo_dir), "branch", "-f", "origin/main"],
        capture_output=True,
        text=True,
    )


# -- mark_done merge verification ---------------------------------------


def test_mark_done_merge_ancestor_succeeds(service, tmp_path):
    """When the feature branch tip is an ancestor of origin/main,
    mark_done succeeds."""
    t = service.create("merge ancestor test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch.
    (repo / "file.txt").write_text("feature work")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "feature commit"],
        capture_output=True,
        text=True,
    )

    # Fast-forward origin/main to include the feature branch (simulate merge).
    _advance_origin_main(repo)

    _comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_merge_squash_detected(service, tmp_path):
    """When the branch tip is NOT an ancestor but a squash-merge commit
    referencing the ticket ID exists on origin/main, mark_done succeeds."""
    t = service.create("squash merge test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch.
    (repo / "file.txt").write_text("feature work")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "feature commit"],
        capture_output=True,
        text=True,
    )

    # Simulate squash-merge: create a new commit on origin/main that
    # references the ticket ID but is NOT a merge of the feature branch.
    _sp.run(
        ["git", "-C", str(repo), "checkout", "origin/main"],
        capture_output=True,
        text=True,
    )
    (repo / "file.txt").write_text("feature work")  # same content
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        [
            "git",
            "-C",
            str(repo),
            "commit",
            "-m",
            f"Squash merge #{t.id} into main",
        ],
        capture_output=True,
        text=True,
    )
    _advance_origin_main(repo)

    _comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_merge_content_match(service, tmp_path):
    """When the branch tip is NOT an ancestor and no log grep match,
    but a changed file on origin/main contains the ticket ID,
    mark_done succeeds (content-level fallback)."""
    t = service.create("content match test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch that includes the ticket ID
    # in file content.
    (repo / "changelog.md").write_text(f"## {t.id}\n\nFeature work done.")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "add changelog"],
        capture_output=True,
        text=True,
    )

    # Simulate a cherry-pick / rebase: apply the same content on
    # origin/main without the ticket ID in the commit message.
    _sp.run(
        ["git", "-C", str(repo), "checkout", "origin/main"],
        capture_output=True,
        text=True,
    )
    (repo / "changelog.md").write_text(f"## {t.id}\n\nFeature work done.")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "apply changelog"],
        capture_output=True,
        text=True,
    )
    _advance_origin_main(repo)

    _comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_merge_not_verified_raises(service, tmp_path):
    """When all merge checks fail, mark_done raises TransitionError."""
    t = service.create("not merged test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_branch(repo, t.id)

    # Make a commit on the feature branch that origin/main does NOT have.
    (repo / "file.txt").write_text("unmerged work")
    import subprocess as _sp

    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "unmerged commit"],
        capture_output=True,
        text=True,
    )

    # origin/main stays at the initial commit — no merge happened.

    with pytest.raises(TransitionError, match="has not been merged"):
        service.mark_done(t.id)


def test_mark_done_merge_no_branch_skips_verification(service, tmp_path):
    """When the feature branch doesn't exist locally, verification is
    skipped and mark_done succeeds (best-effort)."""
    t = service.create("no branch test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    # Create a repo with origin/main but NO feature branch.
    import subprocess as _sp

    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("initial")
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "branch", "origin/main"],
        capture_output=True,
        text=True,
    )

    _comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_rejects_duplicate_changelog_fragments(service, tmp_path):
    """mark_done raises TransitionError when the branch HEAD has
    >1 fragment for the ticket id."""
    t = service.create("dupe frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    _add_fragment(repo, "changes", f"{t.id}.feature.md")
    _add_fragment(repo, "changes", f"{t.id}.misc.md")

    with pytest.raises(TransitionError, match="duplicate changelog fragments"):
        service.mark_done(t.id)


def test_mark_done_allows_single_changelog_fragment(service, tmp_path):
    """mark_done succeeds when only one fragment exists for the ticket."""
    t = service.create("single frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    _add_fragment(repo, "changes", f"{t.id}.misc.md")

    _comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_allows_no_changelog_fragments(service, tmp_path):
    """mark_done succeeds when no fragment exists for the ticket."""
    t = service.create("no frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    # no fragment added

    _comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_mark_done_allows_no_towncrier_config(service, tmp_path):
    """mark_done succeeds (best-effort) when pyproject.toml has no
    [tool.towncrier] section."""
    t = service.create("no tc config")
    ws = service.workspace(t)
    repo = ws.repo_dir

    import subprocess as _sp

    repo.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "-C", str(repo), "init"], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        capture_output=True,
        text=True,
    )
    _sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
    )
    (repo / "pyproject.toml").write_text('[project]\nname = "test"\n')
    _sp.run(["git", "-C", str(repo), "add", "."], capture_output=True, text=True)
    _sp.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        capture_output=True,
        text=True,
    )
    # Add a fragment anyway (no towncrier config → gate should skip).
    _add_fragment(repo, "changes", f"{t.id}.feature.md")

    _comment, ticket = service.mark_done(t.id)
    assert ticket.state is State.DONE


def test_transition_to_done_rejects_duplicate_changelog_fragments(service, tmp_path):
    """transition(..., DONE) raises TransitionError when the branch HEAD
    has >1 fragment for the ticket id."""
    t = service.create("transition dupe frag test")
    ws = service.workspace(t)
    repo = ws.repo_dir

    _setup_repo_with_towncrier(repo)
    _add_fragment(repo, "changes", f"{t.id}.feature.md")
    _add_fragment(repo, "changes", f"{t.id}.misc.md")

    # DRAFT -> DONE is normally allowed; the fragment gate blocks it.
    assert can_transition(t.state, State.DONE)
    with pytest.raises(TransitionError, match="duplicate changelog fragments"):
        service.transition(t.id, State.DONE)
