"""Tests for run_agent_pass behaviour in the shared agent-pass runner."""

from datetime import UTC, datetime, timedelta

from robotsix_mill.agents.runners.pass_runner import (
    _format_recent_proposals,
    _verify_prior_proposals,
    run_agent_pass,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import (
    SourceKind,
    TicketEvent,
    TicketKind,
)
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace
from tests.agents.runners._pass_runner_fixtures import (
    _FakeAgentResult,
    _make_agent,
    _make_settings,
)

# ------------------------------------------------------------------ tests


# 1. Happy path — drafts created, memory persisted
def test_happy_path_drafts_created_and_memory_persisted(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("# Memory v1\n", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="new memory",
        draft_titles=["Fix thing"],
        draft_bodies=["Details about the fix"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        origin_session="test-session-1",
    )

    # Memory persisted to disk
    assert result.updated_memory == "new memory"
    assert memory_file.read_text(encoding="utf-8") == "new memory"

    # Drafts created list
    assert len(result.drafts_created) == 1
    assert "id" in result.drafts_created[0]
    assert result.drafts_created[0]["title"] == "Fix thing"

    # Ticket in DB with correct source and origin_session.
    # Scanner sources (like AUDIT) also get a rollup epic created
    # alongside the draft — filter for the task ticket.
    tickets = service.list()
    draft_tickets = [t for t in tickets if t.kind == TicketKind.TASK]
    assert len(draft_tickets) == 1
    ticket = draft_tickets[0]
    assert ticket.source == "audit"
    assert ticket.origin_session == "test-session-1"
    assert ticket.state == State.DRAFT

    db.reset_engine()


# 2. Empty / missing memory file (first run)
def test_missing_memory_file_first_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "nonexistent.md"
    # File does NOT exist on disk

    captured_memory = []

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        captured_memory.append(memory)
        return _FakeAgentResult(
            updated_memory="initial memory",
            draft_titles=[],
            draft_bodies=[],
        )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label="health",
        service=service,
        settings=settings,
    )

    # Agent received empty string because file didn't exist
    assert captured_memory == [""]

    # Memory was written to disk
    assert result.updated_memory == "initial memory"
    assert memory_file.read_text(encoding="utf-8") == "initial memory"

    db.reset_engine()


# 3. Agent returns zero drafts
def test_agent_returns_zero_drafts(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("some memory", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="some memory",
        draft_titles=[],
        draft_bodies=[],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    assert result.drafts_created == []
    assert memory_file.read_text(encoding="utf-8") == "some memory"

    # No tickets in DB
    tickets = service.list()
    assert len(tickets) == 0

    db.reset_engine()


# 4. Agent returns updated_memory that differs from input
def test_updated_memory_differs_from_input(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="very different",
        draft_titles=[],
        draft_bodies=[],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # File on disk should contain the new value, not the old one
    assert memory_file.read_text(encoding="utf-8") == "very different"
    assert result.updated_memory == "very different"

    db.reset_engine()


# 5. TicketService.create raises — error swallowed, runner continues
def test_service_create_raises_error_swallowed(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("input memory", encoding="utf-8")

    # Monkeypatch service.create to always raise
    monkeypatch.setattr(
        service,
        "create",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    agent_fn = _make_agent(
        updated_memory="persisted memory",
        draft_titles=["Valid draft"],
        draft_bodies=["Valid body"],
    )

    # Runner must NOT propagate the exception
    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # The failing draft is skipped
    assert result.drafts_created == []
    # Memory is still persisted correctly
    assert result.updated_memory == "persisted memory"
    assert memory_file.read_text(encoding="utf-8") == "persisted memory"

    db.reset_engine()


# 6. File I/O error on memory write — error swallowed, result still returned
def test_memory_write_ioerror_swallowed(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old memory", encoding="utf-8")

    # Monkeypatch Path.write_text to raise OSError
    monkeypatch.setattr(
        memory_file.__class__,
        "write_text",
        lambda self, content, encoding=None: (_ for _ in ()).throw(
            OSError("permission denied")
        ),
    )

    agent_fn = _make_agent(
        updated_memory="would-be memory",
        draft_titles=[],
        draft_bodies=[],
    )

    # Runner must NOT propagate the exception
    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # Result still carries the agent's intended memory
    assert result.updated_memory == "would-be memory"
    # The file on disk still has the old content (write failed)
    assert memory_file.read_text(encoding="utf-8") == "old memory"

    db.reset_engine()


# ------------------------------------------------------------------ new tests


# 7. Hermetic verification — synthesised memory + ticket DB
def test_verified_summary_in_agent_prompt(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create three tickets with gap-id markers in different states.

    # Ticket A: CLOSED with DONE event → resolution "merged"
    ta = service.create(
        "Gap A",
        "body A\n\n<!-- audit-gap-id: gap_alpha -->",
        source=SourceKind.AUDIT,
    )
    with db.session(settings, "test-board") as s:
        ticket = s.get(type(ta), ta.id)
        ticket.state = State.DONE
        s.add(TicketEvent(ticket_id=ta.id, state=State.DONE, note="done"))
        s.add(TicketEvent(ticket_id=ta.id, state=State.CLOSED, note="closed"))
        ticket.state = State.CLOSED
        s.commit()

    # Ticket B: CLOSED without DONE → resolution "declined"
    tb = service.create(
        "Gap B",
        "body B\n\n<!-- audit-gap-id: gap_beta -->",
        source=SourceKind.AUDIT,
    )
    with db.session(settings, "test-board") as s:
        ticket = s.get(type(tb), tb.id)
        ticket.state = State.CLOSED
        s.add(TicketEvent(ticket_id=tb.id, state=State.CLOSED, note="closed"))
        s.commit()

    # Ticket C: HUMAN_MR_APPROVAL → resolution "in-flight"
    tc = service.create(
        "Gap C",
        "body C\n\n<!-- audit-gap-id: gap_gamma -->",
        source=SourceKind.AUDIT,
    )
    with db.session(settings, "test-board") as s:
        ticket = s.get(type(tc), tc.id)
        ticket.state = State.HUMAN_MR_APPROVAL
        s.add(
            TicketEvent(
                ticket_id=tc.id, state=State.HUMAN_MR_APPROVAL, note="reviewing"
            )
        )
        s.commit()

    memory_file = tmp_path / "audit_memory.md"
    memory_file.write_text(
        "## Proposals\n- gap_alpha: fix alpha\n- gap_beta: fix beta\n- gap_gamma: fix gamma\n",
        encoding="utf-8",
    )

    captured_memory = []
    captured_verified = []

    def echo_agent(*, settings, memory, recent_proposals="", verified_proposals=""):
        captured_memory.append(memory)
        captured_verified.append(verified_proposals)
        return _FakeAgentResult(
            updated_memory=memory,
            draft_titles=[],
            draft_bodies=[],
        )

    run_agent_pass(
        echo_agent,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # The verified-state context arrives as a one-line summary (not a
    # Markdown table) via the ephemeral ``verified_proposals`` kwarg.
    verified = captured_verified[0]
    assert "prior proposal(s) on file" in verified
    assert "still open" in verified
    assert "read_ticket" in verified

    # And it must NOT have been mixed into the memory the agent sees.
    memory_seen = captured_memory[0]
    assert "## Prior proposals — verified state" not in memory_seen

    db.reset_engine()


# 7b. Regression — verified-state summary is NEVER persisted to memory file
def test_verified_state_not_persisted_to_memory_file(tmp_path):
    """The verified-state summary is an ephemeral DB-derived input that
    must not leak into the persisted memory ledger.  The summary is
    passed as a separate kwarg (not concatenated onto ``memory_text``):
    when the agent echoes memory back via ``updated_memory``, the runner
    persists *only* the agent's own observations — never the
    runner-injected summary.
    """
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create one ticket with a gap-id marker so the runner has
    # something to render in the verified-state summary.
    t = service.create(
        "Some gap",
        "body\n\n<!-- audit-gap-id: some_gap -->",
        source=SourceKind.AUDIT,
    )
    with db.session(settings, "test-board") as s:
        ticket = s.get(type(t), t.id)
        ticket.state = State.DONE
        s.add(TicketEvent(ticket_id=t.id, state=State.DONE, note="done"))
        s.commit()

    memory_file = tmp_path / "audit_memory.md"
    memory_file.write_text(
        "## My own observations\n- never seen the verified summary here.\n",
        encoding="utf-8",
    )

    # The agent echoes whatever memory it saw — this mimics the real
    # audit/health/etc. agents which the prompt instructs to "return
    # the full, updated memory document in ``updated_memory``".
    def echo_agent(*, settings, memory, recent_proposals="", verified_proposals=""):
        return _FakeAgentResult(
            updated_memory=memory,
            draft_titles=[],
            draft_bodies=[],
        )

    run_agent_pass(
        echo_agent,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # The summary must not have been baked into the persisted file —
    # neither on this tick nor (by induction) on any future tick.
    persisted = memory_file.read_text(encoding="utf-8")
    assert "## Prior proposals — verified state" not in persisted
    assert "prior proposal(s) on file" not in persisted
    assert "still open" not in persisted

    db.reset_engine()


# 8. Marker round-trip
def test_marker_round_trip(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old", encoding="utf-8")

    # Create an agent that returns gap_ids
    def agent_with_gap_ids(
        *, settings, memory, recent_proposals="", verified_proposals=""
    ):
        return _FakeAgentResult(
            updated_memory="updated",
            draft_titles=["Fix Z"],
            draft_bodies=["Details"],
            gap_ids=["fix_z"],
        )

    run_agent_pass(
        agent_with_gap_ids,
        memory_file=memory_file,
        source_label="health",
        service=service,
        settings=settings,
    )

    # Now verify the marker was written.
    # Scanner sources (like "health") also get a rollup epic.
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC
    draft_tickets = [t for t in tickets if t.kind == TicketKind.TASK]
    assert len(draft_tickets) == 1
    tid = draft_tickets[0].id
    desc = Workspace(settings.workspaces_dir_for("test-board"), tid).read_description()
    assert "<!-- health-gap-id: fix_z -->" in desc

    # Now call _verify_prior_proposals directly
    mapping = _verify_prior_proposals(service, settings, "health")
    assert "fix_z" in mapping
    assert mapping["fix_z"]["ticket_id"] == tid
    assert mapping["fix_z"]["state"] == "draft"
    assert mapping["fix_z"]["resolution"] == "in-flight"

    db.reset_engine()


# 9. Backwards compatibility — no marker, no crash, no entry
def test_no_marker_ticket_not_in_mapping(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create a ticket WITH the correct source but NO gap-id marker.
    service.create(
        "Old draft",
        "No marker here, just old pre-rollout.",
        source=SourceKind.AUDIT,
    )

    mapping = _verify_prior_proposals(service, settings, SourceKind.AUDIT)
    assert mapping == {}

    db.reset_engine()


# 10. Missing gap_ids attribute — no crash, drafts still created
def test_missing_gap_ids_no_crash(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    # Result object with NO gap_ids attribute at all
    class NoGapIdsResult:
        updated_memory = "mem"
        draft_titles = ["Title"]
        draft_bodies = ["Body"]

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return NoGapIdsResult()

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC
    draft_tickets = [t for t in tickets if t.kind == TicketKind.TASK]
    assert len(draft_tickets) == 1
    desc = Workspace(
        settings.workspaces_dir_for("test-board"), draft_tickets[0].id
    ).read_description()
    # No marker appended
    assert "gap-id:" not in desc

    db.reset_engine()


def test_output_emit_failure_degrades_to_noop(tmp_path):
    """A periodic agent that fails to emit a parseable structured Result
    (pydantic-ai UnexpectedModelBehavior, "Exceeded maximum output
    retries") must NOT hard-error the pass. run_agent_pass degrades to a
    clean no-op: 0 drafts, memory preserved untouched, no ticket created."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("# Prior memory — must survive\n", encoding="utf-8")

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        raise UnexpectedModelBehavior("Exceeded maximum output retries (4)")

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.HEALTH,
        service=service,
        settings=settings,
        origin_session="degrade-session",
    )

    # No drafts, no tickets, memory left exactly as it was.
    assert result.drafts_created == []
    assert result.updated_memory == "# Prior memory — must survive\n"
    assert memory_file.read_text(encoding="utf-8") == "# Prior memory — must survive\n"
    assert service.list() == []

    db.reset_engine()


def test_non_output_exception_still_propagates(tmp_path):
    """The degradation is narrow: a non-output exception (e.g. a bug or a
    forge/clone failure surfacing through the agent) must still raise, not
    be silently swallowed as a no-op."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("m", encoding="utf-8")

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        raise RuntimeError("genuine bug, not an output-emit failure")

    import pytest

    with pytest.raises(RuntimeError, match="genuine bug"):
        run_agent_pass(
            agent_fn,
            memory_file=memory_file,
            source_label=SourceKind.HEALTH,
            service=service,
            settings=settings,
        )

    db.reset_engine()


# ------------------------------------------------------------------ _format_recent_proposals direct tests


class _FakeTicket:
    """Minimal stub matching the Ticket interface used by
    _format_recent_proposals: .id, .state, .title, .created_at."""

    def __init__(self, id, state, title, created_at=None):
        self.id = id
        self.state = state
        self.title = title
        self.created_at = created_at


def test_format_recent_proposals_empty():
    """Empty list returns the '(no recent proposals)' placeholder."""
    result = _format_recent_proposals([])
    assert result == (
        "<recent_proposals>\n[STATE] id | title\n(no recent proposals)\n</recent_proposals>"
    )


def test_format_recent_proposals_single():
    """Single ticket renders one line with the FULL [STATE] id | title."""
    t = _FakeTicket(
        "abc123def456",
        State.DRAFT,
        "Fix bug in thing",
        created_at=datetime.now(UTC),
    )
    result = _format_recent_proposals([t])
    lines = result.split("\n")
    assert lines[0] == "<recent_proposals>"
    assert lines[1] == "[STATE] id | title"
    assert "[draft] abc123def456 | Fix bug in thing" in lines[2]
    assert lines[3] == "</recent_proposals>"


def test_format_recent_proposals_multiple():
    """Multiple tickets render multiple lines, order preserved, full ids."""
    t1 = _FakeTicket(
        "11111112222222", State.DRAFT, "First", created_at=datetime.now(UTC)
    )
    t2 = _FakeTicket(
        "22222223333333", State.CLOSED, "Second", created_at=datetime.now(UTC)
    )
    result = _format_recent_proposals([t1, t2])
    lines = result.split("\n")
    assert lines[0] == "<recent_proposals>"
    assert lines[1] == "[STATE] id | title"
    assert "[draft] 11111112222222 | First" in lines[2]
    assert "[closed] 22222223333333 | Second" in lines[3]
    assert lines[4] == "</recent_proposals>"


def test_format_recent_proposals_full_id_roundtrips_read_ticket_regex():
    """A canonical-format ticket id survives rendering unchanged and still
    matches ``read_ticket._TICKET_ID_RE`` — the bug this ticket fixes was
    truncating the id to a 7-char prefix the regex rejects."""
    from robotsix_mill.agents.read_ticket import _TICKET_ID_RE

    canonical = "20250331T142315Z-add-billing-endpoint-3a1f"
    assert _TICKET_ID_RE.match(canonical) is not None
    t = _FakeTicket(
        canonical,
        State.DRAFT,
        "Add billing endpoint",
        created_at=datetime.now(UTC),
    )
    result = _format_recent_proposals([t])
    assert canonical in result
    # The rendered id (between "[draft] " and " | ") must still match.
    rendered_id = result.split("\n")[2].split("] ", 1)[1].split(" | ", 1)[0]
    assert rendered_id == canonical
    assert _TICKET_ID_RE.match(rendered_id) is not None


def test_format_recent_proposals_states_roundtrip():
    """All common states render their .value correctly."""
    t_draft = _FakeTicket("aaa", State.DRAFT, "draft", created_at=datetime.now(UTC))
    t_closed = _FakeTicket("bbb", State.CLOSED, "closed", created_at=datetime.now(UTC))
    t_done = _FakeTicket("ccc", State.DONE, "done", created_at=datetime.now(UTC))
    result_draft = _format_recent_proposals([t_draft])
    result_closed = _format_recent_proposals([t_closed])
    result_done = _format_recent_proposals([t_done])
    assert "[draft]" in result_draft
    assert "[closed]" in result_closed
    assert "[done]" in result_done


def test_format_recent_proposals_filters_old_tickets():
    """Tickets with created_at older than 7 days are excluded."""
    old = _FakeTicket(
        "old1",
        State.DRAFT,
        "Old draft",
        created_at=datetime.now(UTC) - timedelta(days=10),
    )
    result = _format_recent_proposals([old])
    assert result == (
        "<recent_proposals>\n[STATE] id | title\n(no recent proposals)\n</recent_proposals>"
    )


def test_format_recent_proposals_max_age_days_none():
    """When max_age_days is None, no filtering occurs."""
    old = _FakeTicket(
        "old1",
        State.DRAFT,
        "Old draft",
        created_at=datetime.now(UTC) - timedelta(days=10),
    )
    result = _format_recent_proposals([old], max_age_days=None)
    assert "[draft] old1" in result


def test_format_recent_proposals_mixed_ages():
    """Only recent tickets included when some are old."""
    recent = _FakeTicket(
        "recent1",
        State.DRAFT,
        "Recent",
        created_at=datetime.now(UTC),
    )
    old = _FakeTicket(
        "old1",
        State.CLOSED,
        "Old",
        created_at=datetime.now(UTC) - timedelta(days=10),
    )
    result = _format_recent_proposals([recent, old])
    assert "[draft] recent1" in result
    assert "old1" not in result
    assert "(no recent proposals)" not in result


def test_format_recent_proposals_none_created_at():
    """Ticket with created_at=None is excluded (safety guard)."""
    t = _FakeTicket("id1", State.DRAFT, "No date", created_at=None)
    result = _format_recent_proposals([t])
    assert result == (
        "<recent_proposals>\n[STATE] id | title\n(no recent proposals)\n</recent_proposals>"
    )


def test_format_recent_proposals_all_old():
    """All tickets older than 7 days → placeholder."""
    old1 = _FakeTicket(
        "old1",
        State.DRAFT,
        "Old 1",
        created_at=datetime.now(UTC) - timedelta(days=8),
    )
    old2 = _FakeTicket(
        "old2",
        State.CLOSED,
        "Old 2",
        created_at=datetime.now(UTC) - timedelta(days=15),
    )
    result = _format_recent_proposals([old1, old2])
    assert result == (
        "<recent_proposals>\n[STATE] id | title\n(no recent proposals)\n</recent_proposals>"
    )


def test_format_recent_proposals_empty_list_with_filter():
    """Empty list still returns placeholder even with max_age_days set."""
    result = _format_recent_proposals([], max_age_days=7.0)
    assert result == (
        "<recent_proposals>\n[STATE] id | title\n(no recent proposals)\n</recent_proposals>"
    )


# ------------------------------------------------------------------ _verify_prior_proposals service.list() exception


def test_verify_prior_proposals_service_list_exception(tmp_path, monkeypatch):
    """_verify_prior_proposals returns empty dict when service.list() raises."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    monkeypatch.setattr(
        service,
        "list",
        lambda: (_ for _ in ()).throw(RuntimeError("DB down")),
    )

    result = _verify_prior_proposals(service, settings, SourceKind.AUDIT)
    assert result == {}

    db.reset_engine()


def test_verify_prior_proposals_skips_ticket_with_unresolvable_board(
    tmp_path, monkeypatch
):
    """One CLOSED ticket whose service.history() raises ValueError (board
    no longer resolvable) must NOT abort the whole pass — that ticket is
    skipped while a well-formed matching ticket's entry is still returned."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Healthy CLOSED ticket with a DONE event → resolution "merged".
    good = service.create(
        "Good gap",
        "body\n\n<!-- audit-gap-id: good_gap -->",
        source=SourceKind.AUDIT,
    )
    with db.session(settings, "test-board") as s:
        ticket = s.get(type(good), good.id)
        ticket.state = State.DONE
        s.add(TicketEvent(ticket_id=good.id, state=State.DONE, note="done"))
        s.add(TicketEvent(ticket_id=good.id, state=State.CLOSED, note="closed"))
        ticket.state = State.CLOSED
        s.commit()

    # Orphaned CLOSED ticket whose board can no longer be resolved.
    bad = service.create(
        "Bad gap",
        "body\n\n<!-- audit-gap-id: bad_gap -->",
        source=SourceKind.AUDIT,
    )
    with db.session(settings, "test-board") as s:
        ticket = s.get(type(bad), bad.id)
        ticket.state = State.CLOSED
        s.add(TicketEvent(ticket_id=bad.id, state=State.CLOSED, note="closed"))
        s.commit()

    real_history = service.history

    def fake_history(ticket_id):
        if ticket_id == bad.id:
            raise ValueError(f"Ticket {ticket_id} not found in any configured board")
        return real_history(ticket_id)

    monkeypatch.setattr(service, "history", fake_history)

    # Must not raise — returns a dict skipping the bad ticket.
    result = _verify_prior_proposals(service, settings, SourceKind.AUDIT)
    assert isinstance(result, dict)
    assert "good_gap" in result
    assert result["good_gap"]["ticket_id"] == good.id
    assert result["good_gap"]["resolution"] == "merged"
    assert "bad_gap" not in result

    db.reset_engine()


# ------------------------------------------------------------------ run_agent_pass max_drafts clipping


def test_max_drafts_clips_excess_titles(tmp_path, monkeypatch):
    """Agent returns 5 titles, max_drafts=2 — only 2 tickets created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["T1", "T2", "T3", "T4", "T5"],
        draft_bodies=["B1", "B2", "B3", "B4", "B5"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        max_drafts=2,
    )

    assert len(result.drafts_created) == 2
    tickets = service.list()
    assert len(tickets) == 3  # 2 drafts + 1 rollup EPIC

    db.reset_engine()


def test_max_drafts_gte_titles_no_clipping(tmp_path, monkeypatch):
    """Agent returns 3 titles, max_drafts=5 — all 3 created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["T1", "T2", "T3"],
        draft_bodies=["B1", "B2", "B3"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        max_drafts=5,
    )

    assert len(result.drafts_created) == 3
    tickets = service.list()
    assert len(tickets) == 4  # 3 drafts + 1 rollup EPIC

    db.reset_engine()


def test_max_drafts_none_backward_compat(tmp_path, monkeypatch):
    """max_drafts=None (default) — all 5 titles created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["T1", "T2", "T3", "T4", "T5"],
        draft_bodies=["B1", "B2", "B3", "B4", "B5"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        # max_drafts not passed — defaults to None
    )

    assert len(result.drafts_created) == 5
    tickets = service.list()
    assert len(tickets) == 6  # 5 drafts + 1 rollup EPIC

    db.reset_engine()


# ------------------------------------------------------------------ run_agent_pass empty updated_memory


def test_agent_returns_empty_updated_memory(tmp_path, monkeypatch):
    """When the agent returns '' for updated_memory, persist_memory is
    NOT called and the original memory survives."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old memory", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="",  # empty string from agent
        draft_titles=[],
        draft_bodies=[],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # File on disk unchanged
    assert memory_file.read_text(encoding="utf-8") == "old memory"
    # Result.updated_memory falls back to the pre-agent memory_text
    assert result.updated_memory == "old memory"

    db.reset_engine()


# ------------------------------------------------------------------ mill-internal draft routing


def _multirepo_settings(tmp_path):
    """Build a two-repo registry (audited "test-repo" + mill maintenance
    "robotsix-mill") plus a fresh DB on both boards, mirroring the
    multi-repo deployment topology that run_agent_pass must route
    against."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.config import RepoConfig, ReposRegistry

    _cfg._repos_config = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="t",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
            ),
            "robotsix-mill": RepoConfig(
                repo_id="robotsix-mill",
                board_id="mill-board",
                langfuse_project_name="mill",
                langfuse_public_key="pk2",
                langfuse_secret_key="sk2",
            ),
        }
    )
    db.reset_engine()
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        trace_review_target_repo_id="robotsix-mill",
    )
    db.init_db(settings, board_id="test-board")
    db.init_db(settings, board_id="mill-board")
    return settings


def test_repo_specific_draft_stays_on_audited_board(tmp_path):
    """A draft whose title+body names ONLY audited-repo paths (no
    mill-internal symbols) stays on the audited board — the routing
    heuristic does not false-positive."""
    import robotsix_mill.config as _cfg

    settings = _multirepo_settings(tmp_path)
    audited_svc = TicketService(settings, board_id="test-board")
    mill_svc = TicketService(settings, board_id="mill-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Fix user authentication in src/auth/login.py"],
        draft_bodies=[
            (
                "The login flow in src/auth/login.py drops the session "
                "cookie when the user logs out — patch the cookie clear-out."
            )
        ],
    )

    try:
        run_agent_pass(
            agent_fn,
            memory_file=memory_file,
            source_label=SourceKind.AUDIT,
            service=audited_svc,
            settings=settings,
        )

        # Draft landed on the audited board.
        on_audited = audited_svc.list()
        assert len(on_audited) == 2  # 1 draft + 1 rollup EPIC
        on_audited_drafts = [t for t in on_audited if t.kind == TicketKind.TASK]
        assert len(on_audited_drafts) == 1
        assert (
            on_audited_drafts[0].title == "Fix user authentication in src/auth/login.py"
        )

        # Mill board is empty.
        on_mill = mill_svc.list()
        assert on_mill == []
    finally:
        _cfg._reset_repos_config()
        db.reset_engine()


def test_agent_summary_threads_to_pass_result(tmp_path):
    """The agent's `summary` flows to AgentPassResult.summary, so the run
    registry can show what a 0-draft run actually examined."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("m", encoding="utf-8")

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return _FakeAgentResult(
            updated_memory="m",
            draft_titles=[],
            draft_bodies=[],
            summary="scanned 142 files; 3 clone pairs, 0 above threshold",
        )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )
    assert result.summary == "scanned 142 files; 3 clone pairs, 0 above threshold"
    assert result.drafts_created == []
    db.reset_engine()


# --- combined_verified in agent prompt ---


def test_combined_verified_only_prior_proposals(tmp_path):
    """The prior-proposals verified-state summary is passed to the agent via
    verified_proposals."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create a gap-id ticket
    service.create(
        "Gap ticket",
        "body\n\n<!-- audit-gap-id: some_gap -->",
        source=SourceKind.AUDIT,
    )

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    captured_verified = []

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        captured_verified.append(verified_proposals)
        return _FakeAgentResult(
            updated_memory="mem",
            draft_titles=[],
            draft_bodies=[],
        )

    run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    combined = captured_verified[0]
    assert "prior proposal(s) on file" in combined
    assert "still open" in combined

    db.reset_engine()


# --- strip_ephemeral_sections (prior-proposals verified-state table) ---


# --- run_agent_pass verified_proposals backward compat ---


def test_verified_proposals_still_works_with_module_curator_style(tmp_path):
    """The module_curator agent receives the verified-state summary via
    verified_proposals kwarg — same interface as before."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create context: gap-id ticket
    service.create(
        "Gap",
        "body\n\n<!-- module_curator-gap-id: mc_gap -->",
        source=SourceKind.MODULE_CURATOR,
    )

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    captured = []

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        captured.append(verified_proposals)
        return _FakeAgentResult(
            updated_memory="mem",
            draft_titles=[],
            draft_bodies=[],
        )

    run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
    )

    vp = captured[0]
    assert "prior proposal(s) on file" in vp

    db.reset_engine()
