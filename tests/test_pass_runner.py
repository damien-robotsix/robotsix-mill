"""Tests for the shared agent-pass runner."""

import pytest
from pathlib import Path

from robotsix_mill.pass_runner import run_agent_pass, _verify_prior_proposals
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.core.models import TicketEvent


class _FakeAgentResult:
    """Returned by mock agent callables — matches the interface that
    run_agent_pass accesses: .updated_memory, .draft_titles, .draft_bodies."""

    def __init__(self, updated_memory, draft_titles, draft_bodies,
                 gap_ids=None):
        self.updated_memory = updated_memory
        self.draft_titles = draft_titles
        self.draft_bodies = draft_bodies
        if gap_ids is not None:
            self.gap_ids = gap_ids


def _make_settings(tmp_path, **overrides):
    """Create Settings with MILL_DATA_DIR pointed at tmp_path."""
    overrides.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    return Settings(**overrides)


# ------------------------------------------------------------------ helpers


def _make_agent(updated_memory="new memory", draft_titles=None, draft_bodies=None):
    """Return a callable that returns a _FakeAgentResult with the given data."""
    if draft_titles is None:
        draft_titles = []
    if draft_bodies is None:
        draft_bodies = []

    def agent_fn(*, settings, memory):
        return _FakeAgentResult(
            updated_memory=updated_memory,
            draft_titles=draft_titles,
            draft_bodies=draft_bodies,
        )

    return agent_fn


# ------------------------------------------------------------------ tests


# 1. Happy path — drafts created, memory persisted
def test_happy_path_drafts_created_and_memory_persisted(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

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
        source_label="audit",
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

    # Ticket in DB with correct source and origin_session
    tickets = service.list()
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.source == "audit"
    assert ticket.origin_session == "test-session-1"
    assert ticket.state == State.DRAFT

    db.reset_engine()


# 2. Empty / missing memory file (first run)
def test_missing_memory_file_first_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

    memory_file = tmp_path / "nonexistent.md"
    # File does NOT exist on disk

    captured_memory = []

    def agent_fn(*, settings, memory):
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
    db.init_db(settings)
    service = TicketService(settings)

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
        source_label="audit",
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
    db.init_db(settings)
    service = TicketService(settings)

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
        source_label="audit",
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
    db.init_db(settings)
    service = TicketService(settings)

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
        source_label="audit",
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
    db.init_db(settings)
    service = TicketService(settings)

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old memory", encoding="utf-8")

    # Monkeypatch Path.write_text to raise OSError
    monkeypatch.setattr(
        memory_file.__class__,
        "write_text",
        lambda self, content, encoding=None: (
            (_ for _ in ()).throw(OSError("permission denied"))
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
        source_label="audit",
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
def test_verified_state_table_in_agent_prompt(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

    # Create three tickets with gap-id markers in different states.

    # Ticket A: CLOSED with DONE event → resolution "merged"
    ta = service.create(
        "Gap A", "body A\n\n<!-- audit-gap-id: gap_alpha -->",
        source="audit",
    )
    with db.session(settings) as s:
        ticket = s.get(type(ta), ta.id)
        ticket.state = State.DONE
        s.add(TicketEvent(ticket_id=ta.id, state=State.DONE, note="done"))
        s.add(TicketEvent(ticket_id=ta.id, state=State.CLOSED, note="closed"))
        ticket.state = State.CLOSED
        s.commit()

    # Ticket B: CLOSED without DONE → resolution "declined"
    tb = service.create(
        "Gap B", "body B\n\n<!-- audit-gap-id: gap_beta -->",
        source="audit",
    )
    with db.session(settings) as s:
        ticket = s.get(type(tb), tb.id)
        ticket.state = State.CLOSED
        s.add(TicketEvent(ticket_id=tb.id, state=State.CLOSED, note="closed"))
        s.commit()

    # Ticket C: HUMAN_MR_APPROVAL → resolution "in-flight"
    tc = service.create(
        "Gap C", "body C\n\n<!-- audit-gap-id: gap_gamma -->",
        source="audit",
    )
    with db.session(settings) as s:
        ticket = s.get(type(tc), tc.id)
        ticket.state = State.HUMAN_MR_APPROVAL
        s.add(TicketEvent(ticket_id=tc.id, state=State.HUMAN_MR_APPROVAL, note="reviewing"))
        s.commit()

    memory_file = tmp_path / "audit_memory.md"
    memory_file.write_text(
        "## Proposals\n- gap_alpha: fix alpha\n- gap_beta: fix beta\n- gap_gamma: fix gamma\n",
        encoding="utf-8",
    )

    captured_memory = []

    def echo_agent(*, settings, memory):
        captured_memory.append(memory)
        return _FakeAgentResult(
            updated_memory=memory,
            draft_titles=[],
            draft_bodies=[],
        )

    run_agent_pass(
        echo_agent,
        memory_file=memory_file,
        source_label="audit",
        service=service,
        settings=settings,
    )

    prompt = captured_memory[0]
    assert "## Prior proposals — verified state" in prompt
    assert "gap_alpha" in prompt
    assert "gap_beta" in prompt
    assert "gap_gamma" in prompt
    assert "merged (via DONE)" in prompt
    assert "declined (closed directly)" in prompt
    assert "in-flight" in prompt
    assert "CLOSED" in prompt
    assert "HUMAN_MR_APPROVAL" in prompt

    db.reset_engine()


# 8. Marker round-trip
def test_marker_round_trip(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old", encoding="utf-8")

    # Create an agent that returns gap_ids
    def agent_with_gap_ids(*, settings, memory):
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

    # Now verify the marker was written
    tickets = service.list()
    assert len(tickets) == 1
    tid = tickets[0].id
    desc = Workspace(settings.workspaces_dir, tid).read_description()
    assert "<!-- health-gap-id: fix_z -->" in desc

    # Now call _verify_prior_proposals directly
    mapping = _verify_prior_proposals(service, settings, "health")
    assert "fix_z" in mapping
    assert mapping["fix_z"]["ticket_id"] == tid
    assert mapping["fix_z"]["state"] == "DRAFT"
    assert mapping["fix_z"]["resolution"] == "in-flight"

    db.reset_engine()


# 9. Backwards compatibility — no marker, no crash, no entry
def test_no_marker_ticket_not_in_mapping(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

    # Create a ticket WITH the correct source but NO gap-id marker.
    service.create(
        "Old draft", "No marker here, just old pre-rollout.",
        source="audit",
    )

    mapping = _verify_prior_proposals(service, settings, "audit")
    assert mapping == {}

    db.reset_engine()


# 10. Missing gap_ids attribute — no crash, drafts still created
def test_missing_gap_ids_no_crash(tmp_path):
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    # Result object with NO gap_ids attribute at all
    class NoGapIdsResult:
        updated_memory = "mem"
        draft_titles = ["Title"]
        draft_bodies = ["Body"]

    def agent_fn(*, settings, memory):
        return NoGapIdsResult()

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label="audit",
        service=service,
        settings=settings,
    )

    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 1
    desc = Workspace(settings.workspaces_dir, tickets[0].id).read_description()
    # No marker appended
    assert "gap-id:" not in desc

    db.reset_engine()


# --- memory truncation tests (load_memory) ---

from robotsix_mill.pass_runner import load_memory


def test_load_memory_under_limit_noop(tmp_path):
    """When the memory file is ≤ max_chars, load_memory returns the
    full content unchanged."""
    mf = tmp_path / "memory.md"
    content = "## Entry 1\nSome content\n## Entry 2\nMore content\n"
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=8000)
    assert result == content


def test_load_memory_over_limit_truncates_keep_last(tmp_path, caplog):
    """When the file exceeds max_chars, load_memory keeps the LAST
    entries and prepends a truncation note."""
    import logging

    mf = tmp_path / "memory.md"
    # Build a memory file with dated sections, oldest first.
    sections = []
    for i in range(50):
        sections.append(f"## Entry {i}\nObservation {i}.\n" + ("x" * 200) + "\n")
    content = "\n".join(sections)
    mf.write_text(content, encoding="utf-8")
    assert len(content) > 8000

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=8000)

    # Must be ≤ max_chars + truncation note overhead (~60 chars)
    assert len(result) <= 8000 + 100
    # Must start with the truncation note
    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result.split("\n")[0]
    # Latest entries preserved (Entry 49 should be there)
    assert "Entry 49" in result
    # Earliest entries dropped
    assert "Entry 0" not in result
    # Warning logged
    assert "truncated:" in caplog.text


def test_load_memory_truncation_at_newline_boundary(tmp_path):
    """Truncation happens at a newline boundary so the first kept line
    is a complete line, not a fragment."""
    mf = tmp_path / "memory.md"
    # Build content with clear newline boundaries between entries.
    header = "## Entry old\n" + ("x" * 5000) + "\n"
    middle = "## Entry mid\n" + ("y" * 5000) + "\n"
    tail = "## Entry recent\nRecent observation.\n"
    content = header + middle + tail
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=3000)

    # The first line of the result (after the note) should NOT be a
    # mid-line fragment — it should start at a newline boundary.
    # Verify the note is present and the result contains the recent entry.
    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result.split("\n")[0]
    assert "## Entry recent" in result
    # The result should NOT contain a fragment like "xxxx" or "yyyy"
    # that is not on a complete line starting after a newline —
    # since we advance to the next newline after the cut point.


def test_load_memory_missing_file_ok(tmp_path):
    """load_memory on a non-existent file returns '' (existing behavior)."""
    mf = tmp_path / "nonexistent.md"
    result = load_memory(mf, max_chars=8000)
    assert result == ""


def test_load_memory_max_chars_none_no_truncation(tmp_path):
    """When max_chars is None, no truncation occurs (backward compat)."""
    mf = tmp_path / "memory.md"
    content = "x" * 12000
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=None)
    assert result == content


# --- config default ---


def test_max_memory_chars_default():
    """Bare Settings() has max_memory_chars == 8000."""
    assert Settings().max_memory_chars == 8000
