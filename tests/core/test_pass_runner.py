"""Tests for the shared agent-pass runner."""

from pathlib import Path

from robotsix_mill.pass_runner import (
    run_agent_pass,
    _verify_prior_proposals,
    _GAP_ID_RE,
    load_memory,
    persist_memory,
    _format_recent_proposals,
    _render_verified_table,
    _test_file_exists_for_gap,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.core.models import SourceKind, TicketEvent


class _FakeAgentResult:
    """Returned by mock agent callables — matches the interface that
    run_agent_pass accesses: .updated_memory, .draft_titles, .draft_bodies."""

    def __init__(self, updated_memory, draft_titles, draft_bodies, gap_ids=None):
        self.updated_memory = updated_memory
        self.draft_titles = draft_titles
        self.draft_bodies = draft_bodies
        if gap_ids is not None:
            self.gap_ids = gap_ids


def _make_settings(tmp_path, **overrides):
    """Create Settings with MILL_DATA_DIR pointed at tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Settings(**overrides)


# ------------------------------------------------------------------ helpers


def _make_agent(updated_memory="new memory", draft_titles=None, draft_bodies=None):
    """Return a callable that returns a _FakeAgentResult with the given data."""
    if draft_titles is None:
        draft_titles = []
    if draft_bodies is None:
        draft_bodies = []

    def agent_fn(*, settings, memory, recent_proposals=""):
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
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "nonexistent.md"
    # File does NOT exist on disk

    captured_memory = []

    def agent_fn(*, settings, memory, recent_proposals=""):
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
def test_verified_state_table_in_agent_prompt(tmp_path):
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

    def echo_agent(*, settings, memory, recent_proposals=""):
        captured_memory.append(memory)
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
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old", encoding="utf-8")

    # Create an agent that returns gap_ids
    def agent_with_gap_ids(*, settings, memory, recent_proposals=""):
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
    desc = Workspace(settings.workspaces_dir_for("test-board"), tid).read_description()
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

    def agent_fn(*, settings, memory, recent_proposals=""):
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
    assert len(tickets) == 1
    desc = Workspace(
        settings.workspaces_dir_for("test-board"), tickets[0].id
    ).read_description()
    # No marker appended
    assert "gap-id:" not in desc

    db.reset_engine()


# --- memory truncation tests (load_memory) ---


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


# --- gap-id regex tests for previously-unmatched labels ---


def test_gap_id_re_matches_bespoke_marker():
    """The _GAP_ID_RE must match bespoke:<name> markers so de-duplication works."""
    marker = "<!-- bespoke:my_agent-gap-id: abc123 -->"
    matches = _GAP_ID_RE.findall(marker)
    assert len(matches) == 1
    label, gap_id = matches[0]
    assert label == "bespoke:my_agent"
    assert gap_id == "abc123"


def test_gap_id_re_matches_cost_reconciliation_marker():
    """The _GAP_ID_RE must match cost_reconciliation markers."""
    marker = "<!-- cost_reconciliation-gap-id: 2025-03-15 -->"
    matches = _GAP_ID_RE.findall(marker)
    assert len(matches) == 1
    label, gap_id = matches[0]
    assert label == "cost_reconciliation"
    assert gap_id == "2025-03-15"


# --- test-gap live-filesystem guard tests ---


def test_test_file_exists_skips_ticket(tmp_path, monkeypatch):
    """When the expected test file already exists on disk, the
    test-gap draft is skipped with a warning."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create a fake test file on disk.
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing tests\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/coding.py"],
        draft_bodies=["Add tests for coding.py"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # No tickets created — the draft was skipped.
    assert result.drafts_created == []
    tickets = service.list()
    assert len(tickets) == 0

    db.reset_engine()


def test_test_file_absent_creates_ticket(tmp_path, monkeypatch):
    """When the expected test file does NOT exist, the test-gap draft
    is created normally."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/nonexistent.py"],
        draft_bodies=["Add tests for nonexistent.py"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket IS created.
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 1

    db.reset_engine()


def test_parse_failure_falls_through(tmp_path, monkeypatch):
    """Title that doesn't end in .py conservatively passes through."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for something weird"],
        draft_bodies=["Some body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket IS created (conservative pass-through).
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 1

    db.reset_engine()


def test_repo_dir_none_backward_compat(tmp_path, monkeypatch):
    """With repo_dir=None, the guard is never triggered — all drafts
    are created, matching pre-guard behavior."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create a test file on disk — it should NOT prevent ticket creation
    # since repo_dir is None (guard short-circuits).
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/coding.py"],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        # repo_dir NOT passed (defaults to None)
    )

    # Ticket created despite existing test file because repo_dir is None.
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 1

    db.reset_engine()


def test_non_test_gap_source_never_gated(tmp_path, monkeypatch):
    """SourceKind other than TEST_GAP never triggers the guard,
    regardless of repo_dir."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Existing test file — but source is AUDIT, not TEST_GAP.
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/coding.py"],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket IS created — guard only fires for TEST_GAP.
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 1

    db.reset_engine()


def test_bare_filename_module_path(tmp_path, monkeypatch):
    """Title with a bare filename (no directory) checks the correct path."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create the test file at tests/test_jscpd_tool.py
    test_file = tmp_path / "tests" / "test_jscpd_tool.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for jscpd_tool.py"],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket skipped — test file exists.
    assert result.drafts_created == []
    tickets = service.list()
    assert len(tickets) == 0

    db.reset_engine()


def test_full_path_module_stripping(tmp_path, monkeypatch):
    """Title with src/robotsix_mill/ prefix strips it before checking."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=[
            "test gap: add unit tests for src/robotsix_mill/agents/coding.py"
        ],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket skipped — test file exists after prefix stripping.
    assert result.drafts_created == []
    tickets = service.list()
    assert len(tickets) == 0

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

    def agent_fn(*, settings, memory, recent_proposals=""):
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

    def agent_fn(*, settings, memory, recent_proposals=""):
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


# ------------------------------------------------------------------ persist_memory direct tests


def test_persist_memory_creates_new_file_and_parent_dirs(tmp_path):
    """persist_memory creates the file and parent directories when the
    memory_file does not exist."""
    memory_file = tmp_path / "sub" / "memory.md"
    assert not memory_file.exists()
    persist_memory(memory_file, "hello world")
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == "hello world"


def test_persist_memory_overwrites_existing_file(tmp_path):
    """persist_memory overwrites an existing file with new content."""
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old content", encoding="utf-8")
    persist_memory(memory_file, "new content")
    assert memory_file.read_text(encoding="utf-8") == "new content"


def test_persist_memory_empty_text_file_exists_noop(tmp_path):
    """When text is empty and the file already exists, persist_memory
    does NOT overwrite it."""
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("original", encoding="utf-8")
    persist_memory(memory_file, "")
    assert memory_file.read_text(encoding="utf-8") == "original"


def test_persist_memory_empty_text_file_absent_creates_empty(tmp_path):
    """When text is empty and the file does NOT exist, persist_memory
    creates an empty file."""
    memory_file = tmp_path / "nonexistent.md"
    assert not memory_file.exists()
    persist_memory(memory_file, "")
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == ""


def test_persist_memory_oserror_swallowed(tmp_path, monkeypatch, caplog):
    """persist_memory swallows OSError and logs a warning."""
    import logging

    memory_file = tmp_path / "memory.md"

    def _failing_mkdir(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", _failing_mkdir)

    caplog.set_level(logging.WARNING)
    # Must not raise
    persist_memory(memory_file, "content")
    assert "could not write memory file" in caplog.text


# ------------------------------------------------------------------ _format_recent_proposals direct tests


class _FakeTicket:
    """Minimal stub matching the Ticket interface used by
    _format_recent_proposals: .id, .state, .title."""

    def __init__(self, id, state, title):
        self.id = id
        self.state = state
        self.title = title


def test_format_recent_proposals_empty():
    """Empty list returns the '(no recent proposals)' placeholder."""
    result = _format_recent_proposals([])
    assert result == "<recent_proposals>\n(no recent proposals)\n</recent_proposals>"


def test_format_recent_proposals_single():
    """Single ticket renders one line with [STATE] short_id | title."""
    t = _FakeTicket("abc123def456", State.DRAFT, "Fix bug in thing")
    result = _format_recent_proposals([t])
    lines = result.split("\n")
    assert lines[0] == "<recent_proposals>"
    assert "[draft] abc123d | Fix bug in thing" in lines[1]
    assert lines[2] == "</recent_proposals>"


def test_format_recent_proposals_multiple():
    """Multiple tickets render multiple lines, order preserved."""
    t1 = _FakeTicket("11111112222222", State.DRAFT, "First")
    t2 = _FakeTicket("22222223333333", State.CLOSED, "Second")
    result = _format_recent_proposals([t1, t2])
    lines = result.split("\n")
    assert lines[0] == "<recent_proposals>"
    assert "[draft] 1111111 | First" in lines[1]
    assert "[closed] 2222222 | Second" in lines[2]
    assert lines[3] == "</recent_proposals>"


def test_format_recent_proposals_states_roundtrip():
    """All common states render their .value correctly."""
    t_draft = _FakeTicket("aaa", State.DRAFT, "draft")
    t_closed = _FakeTicket("bbb", State.CLOSED, "closed")
    t_done = _FakeTicket("ccc", State.DONE, "done")
    result_draft = _format_recent_proposals([t_draft])
    result_closed = _format_recent_proposals([t_closed])
    result_done = _format_recent_proposals([t_done])
    assert "[draft]" in result_draft
    assert "[closed]" in result_closed
    assert "[done]" in result_done


# ------------------------------------------------------------------ _render_verified_table direct tests


def test_render_verified_table_empty():
    """Empty dict returns header-only table (no data rows)."""
    result = _render_verified_table({})
    lines = result.split("\n")
    assert "## Prior proposals — verified state" in lines[0]
    assert "| gap_id | ticket_id | state | resolution |" in lines[2]
    assert "|--------|-----------|-------|------------|" in lines[3]
    # No data rows
    assert len(lines) == 4


def test_render_verified_table_merged_resolution():
    """Entry with resolution='merged' shows 'merged (via DONE)'."""
    verified = {
        "gap_1": {
            "ticket_id": "T-123",
            "state": "CLOSED",
            "resolution": "merged",
            "branch": None,
        }
    }
    result = _render_verified_table(verified)
    assert "merged (via DONE)" in result
    assert "T-123" in result


def test_render_verified_table_declined_resolution():
    """Entry with resolution='declined' shows 'declined (closed directly)'."""
    verified = {
        "gap_2": {
            "ticket_id": "T-456",
            "state": "CLOSED",
            "resolution": "declined",
            "branch": None,
        }
    }
    result = _render_verified_table(verified)
    assert "declined (closed directly)" in result
    assert "T-456" in result


def test_render_verified_table_with_branch():
    """Entry with a branch appends ' (branch: ...)' to the ticket_id cell."""
    verified = {
        "gap_3": {
            "ticket_id": "T-789",
            "state": "DRAFT",
            "resolution": "in-flight",
            "branch": "feature/xyz",
        }
    }
    result = _render_verified_table(verified)
    assert "T-789 (branch: feature/xyz)" in result


# ------------------------------------------------------------------ _test_file_exists_for_gap direct tests


def test_test_file_exists_for_gap_non_matching_title(tmp_path):
    """Title that doesn't match the pattern returns False."""
    assert _test_file_exists_for_gap(tmp_path, "some other title") is False


def test_test_file_exists_for_gap_no_py_extension(tmp_path):
    """Title without .py at the end returns False."""
    assert (
        _test_file_exists_for_gap(tmp_path, "test gap: add unit tests for foo/bar")
        is False
    )


def test_test_file_exists_for_gap_file_exists_directory_prefix(tmp_path):
    """Title with directory prefix - file exists → True."""
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# tests", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(
            tmp_path, "test gap: add unit tests for agents/coding.py"
        )
        is True
    )


def test_test_file_exists_for_gap_file_absent(tmp_path):
    """Title points to module with no test file → False."""
    # Ensure the directory exists but NOT the test file
    (tmp_path / "tests" / "agents").mkdir(parents=True, exist_ok=True)
    assert (
        _test_file_exists_for_gap(
            tmp_path, "test gap: add unit tests for agents/missing.py"
        )
        is False
    )


def test_test_file_exists_for_gap_prefix_stripping(tmp_path):
    """Title with src/robotsix_mill/ prefix — prefix stripped before check."""
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# tests", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for src/robotsix_mill/agents/coding.py",
        )
        is True
    )


def test_test_file_exists_for_gap_bare_filename(tmp_path):
    """Bare filename (no directory) checks tests/test_<name>.py."""
    test_file = tmp_path / "tests" / "test_foo.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# tests", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(tmp_path, "test gap: add unit tests for foo.py")
        is True
    )


def test_test_file_exists_for_gap_bare_filename_not_in_subdir(tmp_path):
    """Documenting current limitation: bare filename only checks
    tests/test_<name>.py, NOT tests/subdir/test_<name>.py."""
    # Create file in subdirectory — should NOT be found by current impl
    subdir_file = tmp_path / "tests" / "subdir" / "test_foo.py"
    subdir_file.parent.mkdir(parents=True, exist_ok=True)
    subdir_file.write_text("# tests", encoding="utf-8")
    # The root tests/test_foo.py does NOT exist
    assert (
        _test_file_exists_for_gap(tmp_path, "test gap: add unit tests for foo.py")
        is False
    )


# ------------------------------------------------------------------ load_memory additional edge cases


def test_load_memory_oserror_on_exists(tmp_path, monkeypatch, caplog):
    """OSError on Path.exists() returns '' and logs a warning."""
    import logging

    mf = tmp_path / "memory.md"
    # File does not exist, but exists() itself raises
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: (_ for _ in ()).throw(OSError("disk error")),
    )
    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=8000)
    assert result == ""
    assert "could not read memory file" in caplog.text


def test_load_memory_oserror_on_read_text(tmp_path, monkeypatch, caplog):
    """OSError on read_text() returns '' and logs a warning."""
    import logging

    mf = tmp_path / "memory.md"
    mf.write_text("some content", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, **kw: (_ for _ in ()).throw(OSError("read error")),
    )
    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=8000)
    assert result == ""
    assert "could not read memory file" in caplog.text


def test_load_memory_exact_max_chars_no_truncation(tmp_path, caplog):
    """File length exactly equals max_chars — no truncation, no warning."""
    import logging

    mf = tmp_path / "memory.md"
    content = "a" * 200
    mf.write_text(content, encoding="utf-8")

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=200)
    assert result == content
    # No warning about truncation
    assert "truncated" not in caplog.text


def test_load_memory_one_char_over_truncates(tmp_path, caplog):
    """File is 1 char over max_chars — truncation triggers."""
    import logging

    mf = tmp_path / "memory.md"
    content = "a" * 201  # 1 over
    mf.write_text(content, encoding="utf-8")

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=200)
    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result.split("\n")[0]
    assert "truncated" in caplog.text


def test_load_memory_single_line_over_limit(tmp_path, caplog):
    """File is one long line (no newline), exceeds max_chars.
    Fallback path: keeps text starting at cut_point (no newline to advance to)."""
    import logging

    mf = tmp_path / "memory.md"
    # One long line, no newline at all
    content = "x" * 500
    mf.write_text(content, encoding="utf-8")

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=200)
    assert result.startswith("[... memory truncated:")
    # The kept portion should not be empty
    kept_part = result.split("\n\n", 1)[1] if "\n\n" in result else ""
    assert len(kept_part) > 0


def test_load_memory_trailing_newline_over_limit(tmp_path):
    """File ends with newline, exceeds max_chars — kept portion starts
    on a complete line (not with a blank line from the trailing newline)."""
    mf = tmp_path / "memory.md"
    # Multiple entries, file ends with \n
    prefix = "## Old entry\n" + ("a" * 4000) + "\n"
    suffix = "## Recent entry\nRecent observation.\n"
    content = prefix + suffix
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=2000)
    assert result.startswith("[... memory truncated:")
    # The kept portion should include the recent entry on a proper line
    assert "## Recent entry" in result
    assert "Recent observation." in result
    # The kept text after the truncation note should NOT start with a
    # blank line caused by advancing past a trailing newline
    after_note = result.split("\n\n", 1)[1] if "\n\n" in result else result
    assert not after_note.startswith("\n")


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
    assert len(tickets) == 2

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
    assert len(tickets) == 3

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
    assert len(tickets) == 5

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
