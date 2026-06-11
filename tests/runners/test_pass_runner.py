"""Tests for the shared agent-pass runner."""

from pathlib import Path

from robotsix_mill.runners.pass_runner import (
    run_agent_pass,
    _verify_prior_proposals,
    _render_proposed_actions_table,
    _GAP_ID_RE,
    load_memory,
    persist_memory,
    _format_recent_proposals,
    _render_verified_table,
    _test_file_exists_for_gap,
    _module_curator_premise_check,
    strip_ephemeral_sections,
    ProposedActionItem,
    _verify_proposed_actions,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.core.models import (
    ActionType,
    ProposedAction,
    ProposedActionStatus,
    SourceKind,
    TicketEvent,
)


class _FakeAgentResult:
    """Returned by mock agent callables — matches the interface that
    run_agent_pass accesses: .updated_memory, .draft_titles, .draft_bodies."""

    def __init__(
        self,
        updated_memory,
        draft_titles,
        draft_bodies,
        gap_ids=None,
        summary="",
        proposed_actions=None,
    ):
        self.updated_memory = updated_memory
        self.draft_titles = draft_titles
        self.draft_bodies = draft_bodies
        self.summary = summary
        if gap_ids is not None:
            self.gap_ids = gap_ids
        if proposed_actions is not None:
            self.proposed_actions = proposed_actions


def _make_settings(tmp_path, **overrides):
    """Create Settings with MILL_DATA_DIR pointed at tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Settings(**overrides)


# ------------------------------------------------------------------ helpers


def _make_agent(
    updated_memory="new memory",
    draft_titles=None,
    draft_bodies=None,
    proposed_actions=None,
):
    """Return a callable that returns a _FakeAgentResult with the given data."""
    if draft_titles is None:
        draft_titles = []
    if draft_bodies is None:
        draft_bodies = []

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return _FakeAgentResult(
            updated_memory=updated_memory,
            draft_titles=draft_titles,
            draft_bodies=draft_bodies,
            proposed_actions=proposed_actions,
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

    # The verified-state context arrives in its own ephemeral kwarg,
    # NOT concatenated onto memory — otherwise the agent's
    # ``updated_memory`` echo would round-trip it into the persisted
    # ledger (see ``test_verified_state_not_persisted_to_memory_file``).
    verified = captured_verified[0]
    assert "## Prior proposals — verified state" in verified
    assert "gap_alpha" in verified
    assert "gap_beta" in verified
    assert "gap_gamma" in verified
    assert "merged (via DONE)" in verified
    assert "declined (closed directly)" in verified
    assert "in-flight" in verified
    assert "CLOSED" in verified
    assert "HUMAN_MR_APPROVAL" in verified

    # And it must NOT have been mixed into the memory the agent sees.
    memory_seen = captured_memory[0]
    assert "## Prior proposals — verified state" not in memory_seen

    db.reset_engine()


# 7b. Regression — verified-state table is NEVER persisted to memory file
def test_verified_state_not_persisted_to_memory_file(tmp_path):
    """The verified-state table is an ephemeral DB-derived input that
    must not leak into the persisted memory ledger.  This is the bug
    fixed by passing the table as a separate kwarg (and not concatenating
    it onto ``memory_text``): when the agent echoes memory back via
    ``updated_memory``, the runner persists *only* the agent's own
    observations — never the runner-injected table.
    """
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create one ticket with a gap-id marker so the runner has
    # something to render in the verified-state table.
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
        "## My own observations\n- never seen the verified table here.\n",
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

    # The header must not have been baked into the persisted file —
    # neither on this tick nor (by induction) on any future tick.
    persisted = memory_file.read_text(encoding="utf-8")
    assert "## Prior proposals — verified state" not in persisted

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
    """Single ticket renders one line with the FULL [STATE] id | title."""
    t = _FakeTicket("abc123def456", State.DRAFT, "Fix bug in thing")
    result = _format_recent_proposals([t])
    lines = result.split("\n")
    assert lines[0] == "<recent_proposals>"
    assert "[draft] abc123def456 | Fix bug in thing" in lines[1]
    assert lines[2] == "</recent_proposals>"


def test_format_recent_proposals_multiple():
    """Multiple tickets render multiple lines, order preserved, full ids."""
    t1 = _FakeTicket("11111112222222", State.DRAFT, "First")
    t2 = _FakeTicket("22222223333333", State.CLOSED, "Second")
    result = _format_recent_proposals([t1, t2])
    lines = result.split("\n")
    assert lines[0] == "<recent_proposals>"
    assert "[draft] 11111112222222 | First" in lines[1]
    assert "[closed] 22222223333333 | Second" in lines[2]
    assert lines[3] == "</recent_proposals>"


def test_format_recent_proposals_full_id_roundtrips_read_ticket_regex():
    """A canonical-format ticket id survives rendering unchanged and still
    matches ``read_ticket._TICKET_ID_RE`` — the bug this ticket fixes was
    truncating the id to a 7-char prefix the regex rejects."""
    from robotsix_mill.agents.read_ticket import _TICKET_ID_RE

    canonical = "20250331T142315Z-add-billing-endpoint-3a1f"
    assert _TICKET_ID_RE.match(canonical) is not None
    t = _FakeTicket(canonical, State.DRAFT, "Add billing endpoint")
    result = _format_recent_proposals([t])
    assert canonical in result
    # The rendered id (between "[draft] " and " | ") must still match.
    rendered_id = result.split("\n")[1].split("] ", 1)[1].split(" | ", 1)[0]
    assert rendered_id == canonical
    assert _TICKET_ID_RE.match(rendered_id) is not None


def test_render_board_snapshot_emits_full_id():
    """_render_board_snapshot emits the full t.id (not a 7-char prefix),
    and a canonical-format id still matches read_ticket's regex."""
    from robotsix_mill.runners.periodic_runner import _render_board_snapshot
    from robotsix_mill.agents.read_ticket import _TICKET_ID_RE

    assert _render_board_snapshot([]) == "(no tickets on the board)"

    canonical = "20250331T142315Z-add-billing-endpoint-3a1f"
    t = _FakeTicket(canonical, State.DRAFT, "Add billing endpoint")
    result = _render_board_snapshot([t])
    assert result == f"[draft] {canonical} | Add billing endpoint"
    rendered_id = result.split("] ", 1)[1].split(" | ", 1)[0]
    assert rendered_id == canonical
    assert _TICKET_ID_RE.match(rendered_id) is not None


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


# ------------------------------------------------------------------ mill-internal draft routing


def _multirepo_settings(tmp_path):
    """Build a two-repo registry (audited "test-repo" + mill maintenance
    "robotsix-mill") plus a fresh DB on both boards, mirroring the
    multi-repo deployment topology that run_agent_pass must route
    against."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    import robotsix_mill.config as _cfg

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


def test_mill_internal_draft_routed_to_mill_board(tmp_path):
    """When a periodic agent proposes a draft whose title+body names
    mill-internal symbols and trace_review_target_repo_id resolves to
    a known repo, the draft lands on the mill maintenance board — not
    on the audited repo's board.

    This is the module-curator / code-health incident the routing
    discipline is being extended to cover: every periodic agent that
    audits the audited repo can still propose drafts about mill source
    paths, and those drafts must reach the board that can actually
    implement them."""
    import robotsix_mill.config as _cfg

    settings = _multirepo_settings(tmp_path)
    audited_svc = TicketService(settings, board_id="test-board")
    mill_svc = TicketService(settings, board_id="mill-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Refactor refine stage scope-triage loop"],
        draft_bodies=[
            "The bug lives in src/robotsix_mill/stages/refine.py — "
            "the scope-triage agent loops indefinitely. Fix in stages/."
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

        # Draft landed on the mill maintenance board.
        on_mill = mill_svc.list()
        assert len(on_mill) == 1
        assert on_mill[0].title == "Refactor refine stage scope-triage loop"

        # Audited board is empty — no false-positive duplicate.
        on_audited = audited_svc.list()
        assert on_audited == []
    finally:
        _cfg._reset_repos_config()
        db.reset_engine()


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
            "The login flow in src/auth/login.py drops the session "
            "cookie when the user logs out — patch the cookie clear-out."
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
        assert len(on_audited) == 1
        assert on_audited[0].title == "Fix user authentication in src/auth/login.py"

        # Mill board is empty.
        on_mill = mill_svc.list()
        assert on_mill == []
    finally:
        _cfg._reset_repos_config()
        db.reset_engine()


def test_mill_routing_falls_back_when_target_unset(tmp_path, caplog):
    """When trace_review_target_repo_id is unset, mill-internal drafts
    still file on the audited board (never lose a draft) — and a
    warning is logged."""
    import logging

    settings = _make_settings(tmp_path)
    # NOTE: trace_review_target_repo_id deliberately left at its
    # default ("") to exercise the fallback path.
    assert settings.trace_review_target_repo_id == ""

    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    audited_svc = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Refactor refine stage scope-triage loop"],
        draft_bodies=[
            "The bug lives in src/robotsix_mill/stages/refine.py — "
            "the scope-triage agent loops indefinitely. Fix in stages/."
        ],
    )

    caplog.set_level(logging.WARNING)
    run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=audited_svc,
        settings=settings,
    )

    # Draft still lands on the audited board — no data loss.
    on_audited = audited_svc.list()
    assert len(on_audited) == 1
    assert on_audited[0].title == "Refactor refine stage scope-triage loop"

    # Warning was logged about the unset mill target.
    assert "trace_review_target_repo_id is unset" in caplog.text

    db.reset_engine()


# --- strip_ephemeral_sections (memory ledger hygiene) --------------


def test_strip_removes_prior_proposals_table():

    mem = (
        "## Project layout\n\nStages live in stages/.\n\n"
        "## Prior proposals — verified state\n\n"
        "| gap_id | ticket_id | state | resolution |\n"
        "|--------|-----------|-------|------------|\n"
        "| foo | 20260530Tc57b | CLOSED | merged |\n\n"
        "## Testing conventions\n\nUse pytest.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "Prior proposals" not in out
    assert "20260530Tc57b" not in out
    assert "## Project layout" in out and "## Testing conventions" in out


def test_strip_table_at_end_of_memory():

    mem = "## Patterns\n\nfoo\n\n## Prior proposals — verified state\n\n| a | b |\n"
    out = strip_ephemeral_sections(mem)
    assert "Prior proposals" not in out and "## Patterns" in out


def test_strip_noop_without_table():

    assert strip_ephemeral_sections("## Patterns\nfoo\n").strip() == (
        "## Patterns\nfoo"
    )
    assert strip_ephemeral_sections("") == ""


def test_strip_preserves_prose_after_table():
    """Only the heading + table rows are removed — trailing cross-ticket notes
    (no bounding ## heading) must survive (regression: a whole ledger got
    wiped to empty when prose followed the table)."""

    mem = (
        "## Prior proposals — verified state\n\n"
        "| gap_id | state |\n|--------|-------|\n| foo | CLOSED |\n\n"
        "Real cross-ticket pattern worth keeping.\nA thing to monitor.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "Prior proposals" not in out and "foo | CLOSED" not in out
    assert "Real cross-ticket pattern" in out and "thing to monitor" in out


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


# ==================================================================
# ProposedAction subsystem tests (child 3 — emission)
# ==================================================================


# --- ProposedActionItem model ---


def test_proposed_action_item_fields():
    """ProposedActionItem has exactly four fields with correct types/defaults."""
    pa = ProposedActionItem(
        target_ticket_id="T-123",
        action_type="close",
        rationale="stale — no activity in 60 days",
    )
    assert pa.target_ticket_id == "T-123"
    assert pa.action_type == "close"
    assert pa.payload is None
    assert pa.rationale == "stale — no activity in 60 days"

    # With payload
    pa2 = ProposedActionItem(
        target_ticket_id="T-456",
        action_type="transition",
        payload='{"state": "closed"}',
        rationale="duplicate",
    )
    assert pa2.payload == '{"state": "closed"}'


def test_proposed_action_item_default_factory():
    """proposed_actions defaults to empty list on agent result classes."""
    from robotsix_mill.agents.health import HealthResult

    hr = HealthResult(updated_memory="m", summary="s")
    assert hr.proposed_actions == []


# --- TicketService.create_proposed_action ---


def test_create_proposed_action_pending_row(tmp_path):
    """create_proposed_action creates a PENDING ProposedAction row."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Need a target ticket for FK constraint
    ticket = service.create("Test ticket", "body", source=SourceKind.USER)

    pa = service.create_proposed_action(
        source="health",
        target_ticket_id=ticket.id,
        action_type="close",
        rationale="stale ticket",
    )
    assert pa is not None
    assert pa.source == "health"
    assert pa.target_ticket_id == ticket.id
    assert pa.action_type == ActionType.CLOSE
    assert pa.rationale == "stale ticket"
    assert pa.status == ProposedActionStatus.PENDING
    assert pa.payload is None
    assert pa.id is not None

    db.reset_engine()


def test_create_proposed_action_with_payload(tmp_path):
    """create_proposed_action stores payload as-is."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    ticket = service.create("T", "b", source=SourceKind.USER)
    pa = service.create_proposed_action(
        source="audit",
        target_ticket_id=ticket.id,
        action_type="transition",
        rationale="move to CLOSED",
        payload='{"state": "closed"}',
    )
    assert pa is not None
    assert pa.payload == '{"state": "closed"}'

    db.reset_engine()


def test_create_proposed_action_invalid_action_type(tmp_path, caplog):
    """Invalid action_type returns None and logs a warning."""
    import logging

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    ticket = service.create("T", "b", source=SourceKind.USER)
    caplog.set_level(logging.WARNING)
    pa = service.create_proposed_action(
        source="health",
        target_ticket_id=ticket.id,
        action_type="nonexistent",
        rationale="should fail",
    )
    assert pa is None
    assert "invalid action_type" in caplog.text

    db.reset_engine()


def test_create_proposed_action_nonexistent_target(tmp_path, caplog):
    """Non-existent target_ticket_id still creates the row (SQLite FK
    constraints are not enforced by default). The proposal will be
    caught at execution time — re-validation is the design."""
    import logging

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    caplog.set_level(logging.WARNING)
    pa = service.create_proposed_action(
        source="health",
        target_ticket_id="BOGUS-ID-NEVER-EXISTS",
        action_type="close",
        rationale="should be created — FK not enforced",
    )
    # Row IS created because SQLite FK enforcement is off by default.
    assert pa is not None
    assert pa.status == ProposedActionStatus.PENDING
    # Re-validation will catch this at execution time.

    db.reset_engine()


# --- TicketService.list_proposed_actions ---


def test_list_proposed_actions_excludes_pending_by_default(tmp_path):
    """list_proposed_actions excludes PENDING rows by default."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    ticket = service.create("T", "b", source=SourceKind.USER)
    # Create pending row
    service.create_proposed_action(
        source="health",
        target_ticket_id=ticket.id,
        action_type="close",
        rationale="pending proposal",
    )

    # Default: exclude PENDING → empty
    rows = service.list_proposed_actions(source="health")
    assert rows == []

    # With exclude_status=None → include PENDING
    rows_all = service.list_proposed_actions(source="health", exclude_status=None)
    assert len(rows_all) == 1
    assert rows_all[0].status == ProposedActionStatus.PENDING

    db.reset_engine()


def test_list_proposed_actions_returns_decided_newest_first(tmp_path):
    """list_proposed_actions returns decided rows, newest first."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    ticket = service.create("T1", "b", source=SourceKind.USER)
    # Create one REJECTED and one APPROVED
    pa1 = service.create_proposed_action(
        source="audit",
        target_ticket_id=ticket.id,
        action_type="close",
        rationale="first",
    )
    # Manually update statuses via raw session
    with db.session(settings, "test-board") as s:
        p1 = s.get(ProposedAction, pa1.id)
        p1.status = ProposedActionStatus.REJECTED
        p1.decided_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        p1.decided_by = "alice"
        s.commit()

    ticket2 = service.create("T2", "b2", source=SourceKind.USER)
    pa2 = service.create_proposed_action(
        source="audit",
        target_ticket_id=ticket2.id,
        action_type="transition",
        rationale="second",
    )
    with db.session(settings, "test-board") as s:
        p2 = s.get(ProposedAction, pa2.id)
        p2.status = ProposedActionStatus.APPROVED
        p2.decided_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        p2.decided_by = "bob"
        s.commit()

    rows = service.list_proposed_actions(source="audit")
    assert len(rows) == 2
    # Newest first (pa2 created after pa1)
    assert rows[0].id == pa2.id
    assert rows[1].id == pa1.id

    db.reset_engine()


def test_list_proposed_actions_filters_by_source(tmp_path):
    """list_proposed_actions only returns rows for the given source."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    ticket = service.create("T", "b", source=SourceKind.USER)
    pa_h = service.create_proposed_action(
        source="health",
        target_ticket_id=ticket.id,
        action_type="close",
        rationale="from health",
    )
    pa_a = service.create_proposed_action(
        source="audit",
        target_ticket_id=ticket.id,
        action_type="comment",
        rationale="from audit",
    )

    # Decide both
    with db.session(settings, "test-board") as s:
        for pid in (pa_h.id, pa_a.id):
            p = s.get(ProposedAction, pid)
            p.status = ProposedActionStatus.REJECTED
            p.decided_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
            p.decided_by = "alice"
        s.commit()

    health_rows = service.list_proposed_actions(source="health")
    assert len(health_rows) == 1
    assert health_rows[0].source == "health"

    audit_rows = service.list_proposed_actions(source="audit")
    assert len(audit_rows) == 1
    assert audit_rows[0].source == "audit"

    db.reset_engine()


# --- run_agent_pass persists proposals (step 5b) ---


def test_run_agent_pass_persists_proposed_actions(tmp_path):
    """When the agent result has proposed_actions, each is written as a
    PENDING ProposedAction row."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create target tickets
    t1 = service.create("Target 1", "body1", source=SourceKind.USER)
    t2 = service.create("Target 2", "body2", source=SourceKind.USER)

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    class ResultWithProposals:
        updated_memory = "mem"
        draft_titles = []
        draft_bodies = []
        proposed_actions = [
            ProposedActionItem(
                target_ticket_id=t1.id,
                action_type="close",
                rationale="stale",
            ),
            ProposedActionItem(
                target_ticket_id=t2.id,
                action_type="transition",
                payload='{"state": "closed"}',
                rationale="duplicate",
            ),
        ]

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return ResultWithProposals()

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # No drafts created
    assert result.drafts_created == []

    # Both proposals persisted
    rows = service.list_proposed_actions(source="audit", exclude_status=None)
    assert len(rows) == 2
    assert rows[0].status == ProposedActionStatus.PENDING
    assert rows[1].status == ProposedActionStatus.PENDING

    # Check content
    closes = [r for r in rows if r.action_type == ActionType.CLOSE]
    transitions = [r for r in rows if r.action_type == ActionType.TRANSITION]
    assert len(closes) == 1
    assert closes[0].target_ticket_id == t1.id
    assert closes[0].rationale == "stale"
    assert len(transitions) == 1
    assert transitions[0].target_ticket_id == t2.id
    assert transitions[0].payload == '{"state": "closed"}'

    db.reset_engine()


def test_run_agent_pass_proposed_actions_one_failure_does_not_block_others(
    tmp_path, caplog
):
    """One proposal failing (e.g. invalid action_type) doesn't block others
    or draft creation. Proposals with non-existent targets succeed at
    emission time (SQLite FK not enforced) — they'll be caught at
    execution time."""
    import logging

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    t1 = service.create("Existing", "body", source=SourceKind.USER)

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    class ResultWithProposals:
        updated_memory = "mem"
        draft_titles = ["Draft from pass"]
        draft_bodies = ["Draft body"]
        proposed_actions = [
            ProposedActionItem(
                target_ticket_id=t1.id,
                action_type="close",
                rationale="good proposal",
            ),
            ProposedActionItem(
                target_ticket_id="BOGUS-NONEXISTENT",
                action_type="invalid-action-type",  # this one fails validation
                payload='{"body": "hello"}',
                rationale="bad proposal — invalid action_type",
            ),
        ]

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return ResultWithProposals()

    caplog.set_level(logging.WARNING)
    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    # Draft still created
    assert len(result.drafts_created) == 1

    # Only the good proposal persisted (the invalid one was skipped by
    # create_proposed_action's validation)
    rows = service.list_proposed_actions(source="audit", exclude_status=None)
    assert len(rows) == 1
    assert rows[0].target_ticket_id == t1.id
    assert rows[0].action_type == ActionType.CLOSE

    # Warning logged about the invalid one
    assert "invalid action_type" in caplog.text

    db.reset_engine()


def test_run_agent_pass_no_proposed_actions_field_backward_compat(tmp_path):
    """Agent result without proposed_actions attribute works fine
    (getattr fallback)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old", encoding="utf-8")

    # _FakeAgentResult doesn't have proposed_actions
    agent_fn = _make_agent(
        updated_memory="new",
        draft_titles=["T1"],
        draft_bodies=["B1"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    assert len(result.drafts_created) == 1
    # No proposals — good
    rows = service.list_proposed_actions(source="audit", exclude_status=None)
    assert rows == []

    db.reset_engine()


def test_run_agent_pass_empty_proposed_actions_list_no_rows(tmp_path):
    """Agent returns proposed_actions=[] — zero ProposedAction rows created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    class EmptyResult:
        updated_memory = "mem"
        draft_titles = []
        draft_bodies = []
        proposed_actions = []

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return EmptyResult()

    run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
    )

    rows = service.list_proposed_actions(source="audit", exclude_status=None)
    assert rows == []

    db.reset_engine()


# --- _verify_proposed_actions ---


def test_verify_proposed_actions_returns_decided_only(tmp_path):
    """_verify_proposed_actions only returns decided (non-PENDING) rows."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    ticket = service.create("T", "b", source=SourceKind.USER)

    # PENDING — should NOT appear
    service.create_proposed_action(
        source="health",
        target_ticket_id=ticket.id,
        action_type="close",
        rationale="pending",
    )
    # APPROVED — should appear
    pa_approved = service.create_proposed_action(
        source="health",
        target_ticket_id=ticket.id,
        action_type="transition",
        rationale="approved one",
    )
    with db.session(settings, "test-board") as s:
        p = s.get(ProposedAction, pa_approved.id)
        p.status = ProposedActionStatus.APPROVED
        p.decided_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        p.decided_by = "alice"
        s.commit()

    decided = _verify_proposed_actions(service, SourceKind.HEALTH)
    assert len(decided) == 1
    assert decided[0]["action_type"] == "transition"
    assert decided[0]["status"] == "approved"
    assert decided[0]["rationale"] == "approved one"
    assert decided[0]["decided_by"] == "alice"
    assert decided[0]["decided_at"] != ""

    db.reset_engine()


def test_verify_proposed_actions_empty_when_none(tmp_path):
    """_verify_proposed_actions returns empty list when no decided rows."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    decided = _verify_proposed_actions(service, SourceKind.HEALTH)
    assert decided == []

    db.reset_engine()


def test_verify_proposed_actions_service_error_returns_empty(tmp_path, monkeypatch):
    """_verify_proposed_actions returns empty list on service error."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    monkeypatch.setattr(
        service,
        "list_proposed_actions",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("DB down")),
    )

    decided = _verify_proposed_actions(service, SourceKind.AUDIT)
    assert decided == []

    db.reset_engine()


# --- _render_proposed_actions_table ---


def test_render_proposed_actions_table_empty():
    """Empty list returns empty string."""
    assert _render_proposed_actions_table([]) == ""


def test_render_proposed_actions_table_renders_rows():
    """Renders a Markdown table with the expected columns."""
    decided = [
        {
            "id": 1,
            "target_ticket_id": "20260530Tc57b-abc",
            "action_type": "close",
            "status": "approved",
            "rationale": "stale — no activity in 60 days",
            "decided_at": "2025-06-03T12:00:00",
            "decided_by": "alice",
        },
        {
            "id": 2,
            "target_ticket_id": "20260531Td68c-def",
            "action_type": "transition",
            "status": "rejected",
            "rationale": "still active — recent comment from user",
            "decided_at": "2025-06-03T13:00:00",
            "decided_by": "bob",
        },
    ]
    result = _render_proposed_actions_table(decided)
    assert "## Prior proposed actions — decided" in result
    assert "| id | target_ticket | action | status | decided_by | rationale |" in result
    assert (
        "| 1 | 2026053 | close | approved | alice | stale — no activity in 60 days |"
        in result
    )
    assert (
        "| 2 | 2026053 | transition | rejected | bob | still active — recent comment from user |"
        in result
    )


def test_render_proposed_actions_table_escapes_pipe_in_rationale():
    """Pipe characters in rationale are backslash-escaped."""
    decided = [
        {
            "id": 1,
            "target_ticket_id": "abc1234-xyz",
            "action_type": "comment",
            "status": "approved",
            "rationale": "needs more info | see also T-456",
            "decided_at": "",
            "decided_by": "",
        }
    ]
    result = _render_proposed_actions_table(decided)
    assert "needs more info \\| see also T-456" in result


# --- combined_verified in agent prompt ---


def test_combined_verified_passed_to_agent(tmp_path):
    """Both verified blocks are concatenated and passed to the agent."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create a ticket with gap-id marker for _verify_prior_proposals
    t = service.create(
        "Gap ticket",
        "body\n\n<!-- audit-gap-id: some_gap -->",
        source=SourceKind.AUDIT,
    )

    # Create a decided ProposedAction for _verify_proposed_actions
    pa = service.create_proposed_action(
        source="audit",
        target_ticket_id=t.id,
        action_type="close",
        rationale="stale gap already handled",
    )
    with db.session(settings, "test-board") as s:
        p = s.get(ProposedAction, pa.id)
        p.status = ProposedActionStatus.REJECTED
        p.decided_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        p.decided_by = "alice"
        s.commit()

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
    assert "## Prior proposals — verified state" in combined
    assert "## Prior proposed actions — decided" in combined
    # The prior-proposals table comes before the proposed-actions table
    assert combined.index("## Prior proposals — verified state") < combined.index(
        "## Prior proposed actions — decided"
    )

    db.reset_engine()


def test_combined_verified_only_prior_proposals_when_no_decided_actions(tmp_path):
    """When there are no decided proposed actions, only the
    prior-proposals table is passed (no empty proposed-actions table)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Only create a gap-id ticket, no ProposedAction rows
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
    assert "## Prior proposals — verified state" in combined
    assert "## Prior proposed actions — decided" not in combined

    db.reset_engine()


# --- strip_ephemeral_sections (new proposed-actions table) ---


def test_strip_removes_prior_proposed_actions_table():
    """The ## Prior proposed actions — decided table is stripped from memory."""
    mem = (
        "## Cross-cutting patterns\n\nKeep this.\n\n"
        "## Prior proposed actions — decided\n\n"
        "| id | target_ticket | action | status | decided_by | rationale |\n"
        "|----|---------------|--------|--------|------------|-----------|\n"
        "| 1 | abc1234 | close | approved | alice | stale |\n\n"
        "## More patterns\n\nKeep this too.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "Prior proposed actions" not in out
    assert "abc1234" not in out
    assert "## Cross-cutting patterns" in out
    assert "## More patterns" in out


def test_strip_removes_both_tables():
    """Both ## Prior proposals and ## Prior proposed actions are stripped."""
    mem = (
        "## Intro\n\nKeep.\n\n"
        "## Prior proposals — verified state\n\n"
        "| gap_id | ticket_id | state | resolution |\n"
        "|--------|-----------|-------|------------|\n"
        "| foo | T-1 | CLOSED | merged |\n\n"
        "## Prior proposed actions — decided\n\n"
        "| id | target_ticket | action | status | decided_by | rationale |\n"
        "|----|---------------|--------|--------|------------|-----------|\n"
        "| 1 | abc1234 | close | approved | alice | stale |\n\n"
        "## Outro\n\nKeep.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "Prior proposals" not in out
    assert "Prior proposed actions" not in out
    assert "T-1" not in out
    assert "abc1234" not in out
    assert "## Intro" in out
    assert "## Outro" in out


def test_strip_prior_proposed_actions_at_end_of_memory():
    """Table at the end of memory is removed."""
    mem = "## Patterns\n\nfoo\n\n## Prior proposed actions — decided\n\n| a | b | c | d | e | f |\n"
    out = strip_ephemeral_sections(mem)
    assert "Prior proposed actions" not in out
    assert "## Patterns" in out


def test_strip_prior_proposed_actions_preserves_prose_after_table():
    """Prose after the proposed-actions table survives."""
    mem = (
        "## Prior proposed actions — decided\n\n"
        "| id | target_ticket | action | status | decided_by | rationale |\n"
        "|----|---------------|--------|--------|------------|-----------|\n"
        "| 1 | x | close | rejected | alice | nope |\n\n"
        "Real cross-ticket pattern worth keeping.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "Prior proposed actions" not in out
    assert "Real cross-ticket pattern" in out


def test_strip_noop_without_either_table():
    """No change when neither table is present."""
    assert strip_ephemeral_sections("## Patterns\nfoo\n").strip() == (
        "## Patterns\nfoo"
    )
    assert strip_ephemeral_sections("") == ""


# --- run_agent_pass verified_proposals backward compat ---


def test_verified_proposals_still_works_with_module_curator_style(tmp_path):
    """The module_curator agent receives combined_verified via
    verified_proposals kwarg — same interface as before."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create context: gap-id ticket + decided proposed action
    t = service.create(
        "Gap",
        "body\n\n<!-- module_curator-gap-id: mc_gap -->",
        source=SourceKind.MODULE_CURATOR,
    )
    pa = service.create_proposed_action(
        source="module_curator",
        target_ticket_id=t.id,
        action_type="comment",
        rationale="noted",
    )
    with db.session(settings, "test-board") as s:
        p = s.get(ProposedAction, pa.id)
        p.status = ProposedActionStatus.EXECUTED
        p.decided_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
        p.decided_by = "alice"
        s.commit()

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
    assert "## Prior proposals — verified state" in vp
    assert "## Prior proposed actions — decided" in vp

    db.reset_engine()


# ==================================================================
# module_curator pre-filing premise check
# ==================================================================


def _write_modules_yaml(repo_dir, modules):
    """Write a minimal docs/modules.yaml under *repo_dir*."""
    import yaml as _yaml

    docs = repo_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "modules.yaml").write_text(
        _yaml.safe_dump({"modules": modules}), encoding="utf-8"
    )


# --- suppression: false file-missing premise (the 9c29 case) ---


def test_module_curator_suppresses_false_missing_premise(tmp_path, caplog):
    """A MODULE_CURATOR draft asserting docs/modules.yaml is missing is
    suppressed when the file actually exists under repo_dir."""
    import logging

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # The file the draft claims is missing actually exists.
    _write_modules_yaml(tmp_path, [{"id": "config", "paths": ["src/config.py"]}])

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=[
            "Create docs/modules.yaml: canonical module taxonomy is missing (CRITICAL)"
        ],
        draft_bodies=["docs/modules.yaml does not exist; create it."],
    )

    caplog.set_level(logging.WARNING)
    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert result.drafts_created == []
    assert service.list() == []
    assert "docs/modules.yaml" in caplog.text

    db.reset_engine()


def test_module_curator_files_when_missing_premise_is_true(tmp_path):
    """Counter-example: when the file really is absent, the create draft
    is filed normally (no suppression)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # No docs/modules.yaml on disk.
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=[
            "Create docs/modules.yaml: canonical module taxonomy is missing (CRITICAL)"
        ],
        draft_bodies=["docs/modules.yaml does not exist; create it."],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    assert len(service.list()) == 1

    db.reset_engine()


# --- advisory: stale classify premise (the fdd7 case) ---


def test_module_curator_annotates_stale_classify(tmp_path):
    """A `Classify <file>:` draft whose file does not exist on HEAD is
    filed, but its body carries a [!warning] advisory block."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Classify src/robotsix_mill/gone.py: assign to a module"],
        draft_bodies=["The file src/robotsix_mill/gone.py is unclassified."],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 1
    desc = Workspace(
        settings.workspaces_dir_for("test-board"), tickets[0].id
    ).read_description()
    assert "[!warning]" in desc
    assert "no longer exists on HEAD" in desc

    db.reset_engine()


# --- legitimate classify draft is unaffected ---


def test_module_curator_legitimate_classify_unaffected(tmp_path):
    """A `Classify <file>:` draft for a path that exists on HEAD and is
    NOT yet in docs/modules.yaml is filed unchanged (no advisory)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # File exists on disk; modules.yaml does NOT cover it.
    newfile = tmp_path / "src" / "robotsix_mill" / "newthing.py"
    newfile.parent.mkdir(parents=True, exist_ok=True)
    newfile.write_text("# new\n", encoding="utf-8")
    _write_modules_yaml(tmp_path, [{"id": "config", "paths": ["src/config.py"]}])

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Classify src/robotsix_mill/newthing.py: assign to a module"],
        draft_bodies=["The file src/robotsix_mill/newthing.py is unclassified."],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 1
    desc = Workspace(
        settings.workspaces_dir_for("test-board"), tickets[0].id
    ).read_description()
    assert "[!warning]" not in desc

    db.reset_engine()


# --- advisory: already-classified path ---


def test_module_curator_premise_check_already_classified(tmp_path):
    """A classify draft for a path already covered by a module glob in
    docs/modules.yaml returns an advisory verdict."""
    newfile = tmp_path / "src" / "robotsix_mill" / "agents" / "coding.py"
    newfile.parent.mkdir(parents=True, exist_ok=True)
    newfile.write_text("# code\n", encoding="utf-8")
    _write_modules_yaml(
        tmp_path,
        [{"id": "agents", "paths": ["src/robotsix_mill/agents/*.py"]}],
    )

    verdict = _module_curator_premise_check(
        tmp_path,
        "Classify src/robotsix_mill/agents/coding.py: assign to a module",
        "src/robotsix_mill/agents/coding.py is unclassified.",
    )
    assert verdict is not None
    disposition, note = verdict
    assert disposition == "advisory"
    assert "already classified under module agents" in note


# --- conservative: unmatched / malformed titles return None ---


def test_module_curator_premise_check_unmatched_title_returns_none(tmp_path):
    """A title that matches none of the curator shapes returns None."""
    assert (
        _module_curator_premise_check(
            tmp_path, "Some unrelated free-form title", "body"
        )
        is None
    )


def test_module_curator_premise_check_no_path_returns_none(tmp_path):
    """A create/missing title with no extractable path returns None."""
    assert (
        _module_curator_premise_check(
            tmp_path,
            "Create the missing taxonomy file (CRITICAL)",
            "the taxonomy is missing",
        )
        is None
    )


def test_module_curator_premise_check_malformed_modules_yaml(tmp_path):
    """A malformed docs/modules.yaml never blocks: the helper returns
    None for an existing classify path when YAML parsing fails."""
    newfile = tmp_path / "src" / "robotsix_mill" / "thing.py"
    newfile.parent.mkdir(parents=True, exist_ok=True)
    newfile.write_text("# x\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "modules.yaml").write_text(": : not valid yaml : [", encoding="utf-8")

    verdict = _module_curator_premise_check(
        tmp_path,
        "Classify src/robotsix_mill/thing.py: assign to a module",
        "src/robotsix_mill/thing.py is unclassified.",
    )
    assert verdict is None


# --- guard only fires for MODULE_CURATOR + repo_dir set ---


def test_module_curator_guard_not_fired_for_other_sources(tmp_path):
    """A non-MODULE_CURATOR source with the same 'missing file' title is
    never suppressed, even when the file exists."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    _write_modules_yaml(tmp_path, [{"id": "config", "paths": ["src/config.py"]}])

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Create docs/modules.yaml: missing"],
        draft_bodies=["docs/modules.yaml does not exist"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    assert len(service.list()) == 1

    db.reset_engine()


def test_module_curator_guard_not_fired_without_repo_dir(tmp_path):
    """With repo_dir=None the guard short-circuits — the draft is filed."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Create docs/modules.yaml: missing"],
        draft_bodies=["docs/modules.yaml does not exist"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        # repo_dir not passed -> None
    )

    assert len(result.drafts_created) == 1
    assert len(service.list()) == 1

    db.reset_engine()
