import pytest

from robotsix_mill.agents import retrospecting
from robotsix_mill.agents.retrospecting import RetrospectResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.text_utils import truncate_at_boundary
from robotsix_mill import langfuse_client
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.models import SourceKind
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.retrospect import RetrospectStage


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    s = Settings(**env)
    db.init_db(s)
    from robotsix_mill.config import RepoConfig; return StageContext(settings=s, service=TicketService(s), repo_config=RepoConfig(repo_id="test-repo", board_id="test-board", langfuse_project_name="test", langfuse_public_key="pk-test", langfuse_secret_key="sk-test"))


def _done(ctx):
    t = ctx.service.create("Add X", "spec body")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE):
        ctx.service.transition(t.id, st)
    return ctx.service.get(t.id)


def _no_langfuse(monkeypatch):
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary", lambda s, sid: None
    )


def _default_result(**overrides):
    """Helper: build a RetrospectResult with required fields filled."""
    defaults = dict(
        findings="all good",
        conclusion="closed",
        updated_memory="",
    )
    defaults.update(overrides)
    return RetrospectResult(**defaults)


# --- existing tests updated ---


def test_reviewed_no_draft(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(propose_draft=False),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert len(ctx.service.list()) == 1  # no spawned draft
    assert (ctx.service.workspace(t).artifacts_dir / "retrospect.md").exists()


def test_spawns_linked_draft(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    # The retrospect stage runs inside start_ticket_root_span which sets
    # _current_session to the parent ticket id. Monkeypatch current_session
    # to simulate this.
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "parent-ticket-session-id",
    )
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="wastes tokens",
            conclusion="improvement draft filed",
            propose_draft=True,
            draft_title="Cut retry tokens",
            draft_body="do the thing",
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert "draft" in out.note
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    assert drafts[0].parent_id == t.id  # provenance
    assert drafts[0].title == "Cut retry tokens"
    # origin_session captured from current_session() (the parent ticket's session).
    assert drafts[0].origin_session == "parent-ticket-session-id"


def test_spawned_draft_has_source_retrospect(tmp_path, monkeypatch):
    """Retrospect-spawned drafts have source='retrospect'."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="wastes tokens",
            conclusion="improvement draft filed",
            propose_draft=True,
            draft_title="Cut retry tokens",
            draft_body="do the thing",
        ),
    )
    t = _done(ctx)
    RetrospectStage().run(t, ctx)
    drafts = [x for x in ctx.service.list() if x.parent_id == t.id]
    assert len(drafts) == 1
    assert drafts[0].source == SourceKind.RETROSPECT


def test_spawning_disabled(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, MILL_RETROSPECT_SPAWN_DRAFTS="false")
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="x",
            conclusion="found an issue",
            propose_draft=True,
            draft_title="t",
            draft_body="b",
        ),
    )
    out = RetrospectStage().run(_done(ctx), ctx)
    assert out.next_state is State.CLOSED
    assert "spawning disabled" in out.note
    assert len(ctx.service.list()) == 1  # nothing spawned


def test_agent_error_blocks_resumable(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", boom)
    out = RetrospectStage().run(_done(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note


# --- new tests ---


def test_conclusion_is_transition_note(tmp_path, monkeypatch):
    """The conclusion string is the done -> closed transition note."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            conclusion="pipeline ran cleanly, no issues",
        ),
    )
    out = RetrospectStage().run(_done(ctx), ctx)
    assert out.next_state is State.CLOSED
    assert out.note == "pipeline ran cleanly, no issues"


def test_memory_passed_to_agent(tmp_path, monkeypatch):
    """Memory file contents are passed to the agent."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    memory_file = ctx.settings.memory_file_for("retrospect", ctx.repo_config.board_id if ctx.repo_config else "")
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("## Issue: slow tests\n- ticket-A: 3 retries\n", encoding="utf-8")

    captured_memory = []

    def capture(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)
    RetrospectStage().run(_done(ctx), ctx)
    assert captured_memory == ["## Issue: slow tests\n- ticket-A: 3 retries\n"]


def test_comments_passed_to_agent(tmp_path, monkeypatch):
    """Comments on the ticket are passed to the agent."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    t = _done(ctx)
    ctx.service.add_comment(t.id, "Looks good, but check the tests")
    ctx.service.add_comment(t.id, "Fixed in rebase")

    captured_comments = []

    def capture(**kwargs):
        captured_comments.append(kwargs.get("comments_text", ""))
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)
    RetrospectStage().run(t, ctx)
    text = captured_comments[0]
    assert "Looks good, but check the tests" in text
    assert "Fixed in rebase" in text
    # Each comment line follows the YYYY-MM-DD HH:MM | body pattern
    import re
    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \|", text.split("\n")[0]
    ), f"unexpected first line format: {text.split(chr(10))[0]!r}"


def test_no_comments_passes_empty_string(tmp_path, monkeypatch):
    """When a ticket has no comments, comments_text is empty string."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    captured_comments = []

    def capture(**kwargs):
        captured_comments.append(kwargs.get("comments_text", "NOT_FOUND"))
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)
    RetrospectStage().run(_done(ctx), ctx)
    assert captured_comments == [""]


def test_updated_memory_written_back(tmp_path, monkeypatch):
    """The agent's updated_memory is written back to the file verbatim."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            updated_memory="## Issue: slow tests\n- ticket-A: 3 retries\n- ticket-B: 2 retries\n",
        ),
    )
    RetrospectStage().run(_done(ctx), ctx)
    memory_file = ctx.settings.memory_file_for("retrospect", ctx.repo_config.board_id if ctx.repo_config else "")
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == (
        "## Issue: slow tests\n- ticket-A: 3 retries\n- ticket-B: 2 retries\n"
    )


def test_missing_memory_file_still_closed(tmp_path, monkeypatch):
    """Missing/unreadable memory file → empty string passed, stage still
    reaches CLOSED."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    # Ensure memory file doesn't exist.
    memory_file = ctx.settings.memory_file_for("retrospect", ctx.repo_config.board_id if ctx.repo_config else "")
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def capture(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)
    out = RetrospectStage().run(_done(ctx), ctx)
    assert out.next_state is State.CLOSED
    assert captured_memory == [""]


def test_unreadable_memory_file_still_closed(tmp_path, monkeypatch):
    """Unreadable memory file (OSError) → empty string, still reaches CLOSED."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    class _UnreadableFile:
        def exists(self):
            return True
        def read_text(self, **kwargs):
            raise OSError("permission denied")

    monkeypatch.setattr(
        ctx.settings.__class__, "retrospect_memory_file",
        property(lambda self: _UnreadableFile()),
    )

    captured_memory = []

    def capture(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)
    out = RetrospectStage().run(_done(ctx), ctx)
    assert out.next_state is State.CLOSED
    assert captured_memory == [""]


def test_draft_spawn_only_when_agent_proposes_and_enabled(tmp_path, monkeypatch):
    """Draft is spawned only when the agent proposes one AND
    MILL_RETROSPECT_SPAWN_DRAFTS is on.  Parent is set to the current ticket."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    # Agent proposes a draft — spawn should fire.
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="should spawn",
            conclusion="spawning improvement",
            propose_draft=True,
            draft_title="Fix X",
            draft_body="details",
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    assert drafts[0].parent_id == t.id
    assert drafts[0].title == "Fix X"


def test_no_draft_when_memory_not_sufficient(tmp_path, monkeypatch):
    """When the agent does NOT propose a draft (memory not sufficient),
    no draft is spawned even though spawning is enabled."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="minor issue, not enough evidence yet",
            conclusion="noted, insufficient evidence",
            propose_draft=False,
        ),
    )
    out = RetrospectStage().run(_done(ctx), ctx)
    assert out.next_state is State.CLOSED
    assert len(ctx.service.list()) == 1  # no draft spawned


def test_memory_default_path_derives_from_data_dir(tmp_path, monkeypatch):
    """When MILL_RETROSPECT_MEMORY_PATH is not set, the path derives from data_dir."""
    ctx = _ctx(tmp_path)
    board = ctx.repo_config.board_id if ctx.repo_config else ""
    expected = (
        ctx.settings.data_dir / board / "retrospect_memory.md"
        if board
        else ctx.settings.data_dir / "retrospect_memory.md"
    )
    assert ctx.settings.memory_file_for("retrospect", board) == expected


def test_noop_draft_is_not_spawned(tmp_path, monkeypatch):
    """Regression: the model sometimes sets propose_draft=true with a
    'No notable issues - clean run' title. That must NOT create a
    ticket — the board stays clean; analysis lives in findings."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="clean run, nothing notable",
            conclusion="clean",
            propose_draft=True,
            draft_title="No notable issues - clean run",
            draft_body="Everything looks fine, no action needed.",
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert len(ctx.service.list()) == 1  # the no-op draft was dropped


def test_is_noop_draft_helper():
    from robotsix_mill.stages.retrospect import _is_noop_draft
    real_body = "Problem: X retries 5x. Fix: cap at 2 in retry.py."
    assert _is_noop_draft("No notable issues - clean run", real_body)
    assert _is_noop_draft("Clean ticket, no issues to flag", real_body)
    assert _is_noop_draft("Nothing to report", real_body)
    assert _is_noop_draft("", real_body)   # empty title
    assert _is_noop_draft(None, real_body)
    # Title-only: legitimately terse tickets are NOT flagged.
    assert not _is_noop_draft("Cut retry tokens", "do the thing")
    assert not _is_noop_draft(
        "Cap transient retries at 2 in agents/retry.py", real_body
    )


# --- truncate_at_boundary tests ---


def test_truncate_noop_when_within_limit():
    """Text ≤ max_chars is returned unchanged with no indicator."""
    text = "Short description."
    assert truncate_at_boundary(text, 6000) == text


def test_truncate_at_sentence_boundary():
    """A ~6100-char description with a sentence boundary at ~5950
    truncates at the boundary, not at the hard 6000 limit."""
    # Build a description where the last ". " before 6000 is at ~5950.
    prefix = "A" * 5948 + ". "  # sentence boundary ends at position 5950
    suffix = "B" * 200           # pushes total well past 6000
    text = prefix + suffix       # len ≈ 6150
    result = truncate_at_boundary(text, 6000)
    # Should have truncated at the ". " boundary (position 5950).
    assert result.startswith("A" * 5948 + ".")
    assert "[... description truncated;" in result
    assert "chars omitted]" in result
    # Hard truncation at 6000 would have included some B's; verify none appear.
    assert "B" not in result


def test_truncation_indicator_appended():
    """When truncation occurs, the indicator with the correct count is appended."""
    text = "Sentence one. Sentence two." + "X" * 6100
    result = truncate_at_boundary(text, 6000)
    assert "[... description truncated;" in result
    # The omitted count should equal len(text) minus the cut position.
    # Extract the number from the indicator.
    import re
    m = re.search(r"\[\.\.\. description truncated; (\d+) chars omitted\]", result)
    assert m is not None
    omitted = int(m.group(1))
    # The truncated portion (before indicator) should be shorter than original.
    truncated_body = result[: result.index("[... description truncated;")]
    assert omitted == len(text) - len(truncated_body.rstrip("\n"))


# --- _check_memory_count_consistency unit tests ---


def test_count_drift_detected():
    """Assessment says 'Eleven tickets' but only 10 distinct IDs → warning."""
    from robotsix_mill.stages.retrospect import _check_memory_count_consistency

    memory = (
        "## Issue: Slow test suite\n"
        "**Assessment:** Eleven tickets now demonstrate this pattern.\n"
        "**Evidence:**\n"
        + "\n".join(f"- `TKT-{i:03d}`" for i in range(1, 11))
        + "\n"
    )
    warnings = _check_memory_count_consistency(memory)
    assert len(warnings) == 1
    assert "Slow test suite" in warnings[0]
    assert "claims 11 ticket" in warnings[0]
    assert "has 10 distinct" in warnings[0]


def test_count_match_no_warning():
    """Assessment says '3 tickets' and exactly 3 IDs → empty list."""
    from robotsix_mill.stages.retrospect import _check_memory_count_consistency

    memory = (
        "## Issue: Fragile retry logic\n"
        "**Assessment:** 3 tickets now demonstrate this.\n"
        "**Evidence:**\n"
        "- `TKT-AAA`\n"
        "- `TKT-BBB`\n"
        "- `TKT-CCC`\n"
    )
    warnings = _check_memory_count_consistency(memory)
    assert warnings == []


def test_no_numeric_count_no_warning():
    """Assessment with no numeric count → empty list (no false positive)."""
    from robotsix_mill.stages.retrospect import _check_memory_count_consistency

    memory = (
        "## Issue: Token waste\n"
        "**Assessment:** Multiple tickets show this pattern.\n"
        "**Evidence:**\n"
        "- `TKT-A`\n"
        "- `TKT-B`\n"
    )
    warnings = _check_memory_count_consistency(memory)
    assert warnings == []


def test_empty_memory_no_crash():
    """Empty or near-empty memory → empty list, no crash."""
    from robotsix_mill.stages.retrospect import _check_memory_count_consistency

    assert _check_memory_count_consistency("") == []
    assert _check_memory_count_consistency("   \n  ") == []
    assert _check_memory_count_consistency("just some notes, no structure") == []


def test_multiple_issues_mixed():
    """Memory with multiple issues: one drifted, one consistent, one no-count."""
    from robotsix_mill.stages.retrospect import _check_memory_count_consistency

    memory = (
        "## Issue: Slow tests\n"
        "**Assessment:** 5 tickets show this.\n"
        "- `TKT-1`\n"
        "- `TKT-2`\n"
        "- `TKT-3`\n"
        "\n"
        "## Issue: Token waste\n"
        "**Assessment:** Four tickets now demonstrate.\n"
        "- `TKT-A`\n"
        "- `TKT-B`\n"
        "- `TKT-C`\n"
        "\n"
        "## Issue: Flaky lint\n"
        "**Assessment:** Several tickets affected.\n"
        "- `TKT-X`\n"
    )
    warnings = _check_memory_count_consistency(memory)
    # Both "Slow tests" (claims 5, has 3) and "Token waste" (claims 4, has 3) drifted.
    assert len(warnings) == 2
    assert any("Slow tests" in w and "claims 5 ticket" in w and "has 3 distinct" in w
               for w in warnings)
    assert any("Token waste" in w and "claims 4 ticket" in w and "has 3 distinct" in w
               for w in warnings)


def test_word_number_parsing():
    """Word numbers like 'ten', 'Twenty' are parsed correctly."""
    from robotsix_mill.stages.retrospect import _check_memory_count_consistency

    # "Ten tickets" claim with exactly 10 IDs → no warning
    memory = (
        "## Issue: Slow CI\n"
        "**Assessment:** Ten tickets demonstrate this.\n"
        + "\n".join(f"- `TKT-{i}`" for i in range(10))
        + "\n"
    )
    assert _check_memory_count_consistency(memory) == []

    # "twenty tickets" claim with 19 IDs → warning
    memory2 = (
        "## Issue: Memory leak\n"
        "**Assessment:** Twenty tickets show the leak.\n"
        + "\n".join(f"- `TKT-{i}`" for i in range(19))
        + "\n"
    )
    warnings = _check_memory_count_consistency(memory2)
    assert len(warnings) == 1
    assert "claims 20 ticket" in warnings[0]
    assert "has 19 distinct" in warnings[0]


def test_compound_word_number_parsing():
    """Compound word numbers like 'twenty-one', 'ninety-nine' are parsed."""
    from robotsix_mill.stages.retrospect import _check_memory_count_consistency

    # "Twenty-one tickets" claim with exactly 21 IDs → no warning
    memory = (
        "## Issue: Slow CI\n"
        "**Assessment:** Twenty-one tickets now demonstrate…\n"
        + "\n".join(f"- `TKT-{i}`" for i in range(21))
        + "\n"
    )
    assert _check_memory_count_consistency(memory) == []

    # "Twenty-one tickets" claim with 22 IDs → warning
    memory2 = (
        "## Issue: Memory leak\n"
        "**Assessment:** Twenty-one tickets now demonstrate…\n"
        + "\n".join(f"- `TKT-{i}`" for i in range(22))
        + "\n"
    )
    warnings = _check_memory_count_consistency(memory2)
    assert len(warnings) == 1
    assert "claims 21 ticket" in warnings[0]
    assert "has 22 distinct" in warnings[0]

    # "ninety-nine tickets" claim with exactly 99 IDs → no warning
    memory3 = (
        "## Issue: Big pattern\n"
        "**Assessment:** ninety-nine tickets show the pattern.\n"
        + "\n".join(f"- `TKT-{i}`" for i in range(99))
        + "\n"
    )
    assert _check_memory_count_consistency(memory3) == []


# ---------------------------------------------------------------------------
# draft_gap_id marker injection tests
# ---------------------------------------------------------------------------


def test_draft_gap_id_marker_injected_on_spawn(tmp_path, monkeypatch):
    """When the agent returns draft_gap_id, the marker is appended to
    the draft description."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="pattern found",
            conclusion="filing draft",
            propose_draft=True,
            draft_title="Fix token waste in explore",
            draft_body="Reduce duplicate reads.",
            draft_gap_id="token_waste_explore",
        ),
    )
    t = _done(ctx)
    RetrospectStage().run(t, ctx)
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    # Read the ticket description via workspace.
    desc = ctx.service.workspace(drafts[0]).read_description()
    assert "<!-- retrospect-gap-id: token_waste_explore -->" in desc


def test_no_marker_when_draft_gap_id_is_none(tmp_path, monkeypatch):
    """When draft_gap_id is None, no marker is injected."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="pattern found",
            conclusion="filing draft",
            propose_draft=True,
            draft_title="Fix token waste in explore",
            draft_body="Reduce duplicate reads.",
            draft_gap_id=None,
        ),
    )
    t = _done(ctx)
    RetrospectStage().run(t, ctx)
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    desc = ctx.service.workspace(drafts[0]).read_description()
    assert "<!-- retrospect-gap-id:" not in desc


def test_verified_state_block_in_memory(tmp_path, monkeypatch):
    """When a prior retrospect-spawned draft exists and is CLOSED with DONE,
    the verified-state table appears in the memory passed to the agent."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    # Create a CLOSED ticket with source=retrospect and a gap-id marker.
    draft = ctx.service.create(
        "Fix slow CI",
        "Speed up CI.\n\n<!-- retrospect-gap-id: slow_ci -->",
        source=SourceKind.RETROSPECT,
    )
    # Transition it to DONE then CLOSED (simulates a merged draft).
    ctx.service.transition(draft.id, State.READY)
    ctx.service.transition(draft.id, State.DELIVERABLE)
    ctx.service.transition(draft.id, State.IMPLEMENT_COMPLETE)
    ctx.service.transition(draft.id, State.HUMAN_MR_APPROVAL)
    ctx.service.transition(draft.id, State.DONE)
    ctx.service.transition(draft.id, State.CLOSED)

    captured_memory = []

    def capture(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    t = _done(ctx)
    RetrospectStage().run(t, ctx)
    assert len(captured_memory) == 1
    mem = captured_memory[0]
    assert "## Prior proposals — verified state" in mem
    assert "slow_ci" in mem
    assert "merged" in mem


def test_verify_prior_proposals_no_crash_on_markerless_retrospect_draft(tmp_path):
    """A retrospect-sourced ticket without a gap-id marker does not appear
    in the mapping and does not raise an error."""
    from robotsix_mill.pass_runner import _verify_prior_proposals

    db.reset_engine()
    s = Settings(MILL_DATA_DIR=str(tmp_path / "data"))
    db.init_db(s)
    svc = TicketService(s)

    # Create a retrospect-sourced ticket with NO gap-id marker.
    ticket = svc.create("Old retrospect draft", "No marker here.", source=SourceKind.RETROSPECT)
    # Move it to CLOSED (with DONE in history).
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE, State.CLOSED):
        svc.transition(ticket.id, st)

    result = _verify_prior_proposals(svc, s, SourceKind.RETROSPECT)
    # The marker-less ticket must not appear.
    assert ticket.id not in [v["ticket_id"] for v in result.values()]
    # No error raised — we got here fine.


# ---------------------------------------------------------------------------
# follow-up (concrete incomplete-work detection) tests
# ---------------------------------------------------------------------------


def test_follow_up_spawned_for_stub(tmp_path, monkeypatch):
    """Agent returns follow_up_title/follow_up_body → stage creates a
    DRAFT with source='retrospect' and parent_id set to the completed ticket."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="stub found",
            conclusion="follow-up filed",
            follow_up_title="Wire real doc agent in DocumentStage._run_doc_agent",
            follow_up_body="The _run_doc_agent method is a no-op stub. Wire the real agent.",
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    assert drafts[0].source == SourceKind.RETROSPECT
    assert drafts[0].parent_id == t.id
    assert drafts[0].title == "Wire real doc agent in DocumentStage._run_doc_agent"


def test_follow_up_dedup_skips_duplicate(tmp_path, monkeypatch):
    """A non-terminal ticket with the same title (case-insensitive)
    already exists → _maybe_spawn_follow_up returns None, no duplicate."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    # Pre-create an open ticket with the same title (different case).
    existing = ctx.service.create(
        "Wire real doc agent in DocumentStage._run_doc_agent",
        "Existing stub ticket.",
    )
    # Leave in DRAFT (default) — an open, non-terminal ticket.

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="stub found",
            conclusion="follow-up already filed",
            follow_up_title="WIRE REAL DOC AGENT IN DocumentStage._run_doc_agent",
            follow_up_body="The _run_doc_agent method is a no-op stub.",
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    # Only the pre-existing ticket + the completed ticket, no extra draft.
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    assert drafts[0].id == existing.id


def test_follow_up_not_spawned_for_clean_ticket(tmp_path, monkeypatch):
    """Agent returns no follow-up (both fields None) → nothing created,
    board stays clean."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="clean run",
            conclusion="closed",
            follow_up_title=None,
            follow_up_body=None,
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert len(ctx.service.list()) == 1  # only the completed ticket


def test_follow_up_in_artifact_and_note(tmp_path, monkeypatch):
    """When a follow-up is spawned, its ID appears in retrospect.md and
    the transition note."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="stub found",
            conclusion="follow-up filed",
            follow_up_title="Wire real doc agent in DocumentStage._run_doc_agent",
            follow_up_body="The _run_doc_agent method is a no-op stub.",
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)

    # Transition note includes follow-up ID.
    assert "follow-up" in out.note

    # Artifact includes follow-up line.
    artifact = (ctx.service.workspace(t).artifacts_dir / "retrospect.md").read_text()
    assert "follow-up:" in artifact
    assert "—" not in artifact.split("follow-up:")[1].split("\n")[0]


def test_follow_up_dedup_allows_refile_when_closed(tmp_path, monkeypatch):
    """An existing ticket with the same title is CLOSED → dedup does NOT
    block (regression is worth re-filing)."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    # Create a CLOSED ticket with the same title.
    closed_ticket = ctx.service.create(
        "Wire real doc agent in DocumentStage._run_doc_agent",
        "Already closed.",
    )
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE, State.CLOSED):
        ctx.service.transition(closed_ticket.id, st)

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="stub still present",
            conclusion="follow-up re-filed",
            follow_up_title="Wire real doc agent in DocumentStage._run_doc_agent",
            follow_up_body="The stub is still there.",
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    # A new draft should be created (re-filed) because the matching one was CLOSED.
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    assert drafts[0].parent_id == t.id  # the new follow-up


def test_systemic_proposals_still_work(tmp_path, monkeypatch):
    """Regression: propose_draft path still creates drafts exactly as
    before — no interaction between the two mechanisms."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "parent-ticket-session-id",
    )
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="wastes tokens",
            conclusion="improvement draft filed",
            propose_draft=True,
            draft_title="Cut retry tokens",
            draft_body="do the thing",
            follow_up_title=None,
            follow_up_body=None,
        ),
    )
    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert "draft" in out.note
    drafts = [x for x in ctx.service.list() if x.state is State.DRAFT]
    assert len(drafts) == 1
    assert drafts[0].parent_id == t.id
    assert drafts[0].title == "Cut retry tokens"
    assert drafts[0].source == SourceKind.RETROSPECT


# ---------------------------------------------------------------------------
# epic / sibling context passthrough tests
# ---------------------------------------------------------------------------


def test_epic_context_passed_to_retrospect_agent(tmp_path, monkeypatch):
    """When a child of an epic is retrospected, epic_context is non-empty
    and contains the parent epic's description."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    # Create an epic parent.
    epic = ctx.service.create(
        "Epic: Configuration system overhaul",
        "Deliver a unified config system across all stages.",
        kind="epic",
    )
    # Create a child ticket linked to the epic.
    child = ctx.service.create(
        "Wire config loader in refine stage",
        "Connect the YAML loader.",
        parent_id=epic.id,
    )
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE):
        ctx.service.transition(child.id, st)
    child = ctx.service.get(child.id)

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    RetrospectStage().run(child, ctx)
    ec = captured.get("epic_context", "")
    assert ec, "epic_context should be non-empty for child of epic"
    assert "````epic-context" in ec
    assert "unified config system" in ec


def test_sibling_context_passed_to_retrospect_agent(tmp_path, monkeypatch):
    """When a child of an epic has siblings, sibling_context lists them
    (ID, state, title) but excludes the current ticket."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    epic = ctx.service.create("Epic: Multi-stage refactor", "Big refactor.", kind="epic")

    current = ctx.service.create("Refactor refine stage", "desc", parent_id=epic.id)
    sibling_a = ctx.service.create("Refactor implement stage", "desc", parent_id=epic.id)
    sibling_b = ctx.service.create("Refactor retrospect stage", "desc", parent_id=epic.id)

    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE):
        ctx.service.transition(current.id, st)
    current = ctx.service.get(current.id)

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    RetrospectStage().run(current, ctx)
    sc = captured.get("sibling_context", "")
    assert sc, "sibling_context should be non-empty when siblings exist"
    assert "<epic_siblings>" in sc
    assert sibling_a.id in sc
    assert sibling_b.id in sc
    assert current.id not in sc  # current ticket excluded
    assert "draft" in sc.lower() or "[draft]" in sc  # state listed


def test_no_epic_context_for_standalone_ticket(tmp_path, monkeypatch):
    """Ticket with no parent → epic_context and sibling_context are both empty."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    RetrospectStage().run(_done(ctx), ctx)
    assert captured.get("epic_context", "") == ""
    assert captured.get("sibling_context", "") == ""


def test_no_epic_context_for_non_epic_parent(tmp_path, monkeypatch):
    """Ticket whose parent is not an epic → epic_context is empty
    (get_epic_context returns '' for non-epic parents)."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    parent = ctx.service.create("Regular parent ticket", "Not an epic.")
    child = ctx.service.create("Child of regular ticket", "desc", parent_id=parent.id)
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE):
        ctx.service.transition(child.id, st)
    child = ctx.service.get(child.id)

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    RetrospectStage().run(child, ctx)
    assert captured.get("epic_context", "") == ""
    # Child has no siblings, so sibling_context should be empty too.
    assert captured.get("sibling_context", "") == ""


def test_follow_up_suppressed_when_sibling_covers_gap(tmp_path, monkeypatch):
    """When the agent suppresses follow_up because a sibling covers the
    gap, no follow-up ticket is created — integration test of agent
    decision flowing through the stage."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    epic = ctx.service.create("Epic: Doc system", "Wire doc agent + tests.", kind="epic")
    current = ctx.service.create("Wire doc agent stub", "desc", parent_id=epic.id)
    ctx.service.create("Doc agent unit tests", "desc", parent_id=epic.id)

    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE):
        ctx.service.transition(current.id, st)
    current = ctx.service.get(current.id)

    # The agent sees a stub but notes a sibling covers it — no follow_up.
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(
            findings="_run_doc_agent is a stub, deferred to sibling TKT-...",
            conclusion="closed — gap deferred to sibling",
            follow_up_title=None,
            follow_up_body=None,
        ),
    )

    out = RetrospectStage().run(current, ctx)
    assert out.next_state is State.CLOSED
    # No follow-up spawned as a child of the current ticket.
    spawned = [x for x in ctx.service.list() if x.parent_id == current.id]
    assert len(spawned) == 0


def test_sibling_context_empty_when_no_other_children(tmp_path, monkeypatch):
    """Epic parent with only the current child → sibling_context is empty."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    epic = ctx.service.create("Epic: Solo mission", "Just one child.", kind="epic")
    child = ctx.service.create("The only child", "desc", parent_id=epic.id)
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE):
        ctx.service.transition(child.id, st)
    child = ctx.service.get(child.id)

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    RetrospectStage().run(child, ctx)
    assert captured.get("sibling_context", "") == ""


def test_sibling_title_truncated_at_80_chars(tmp_path, monkeypatch):
    """Sibling titles > 80 chars are truncated with '…' in the sibling list."""
    ctx = _ctx(tmp_path)
    _no_langfuse(monkeypatch)

    epic = ctx.service.create("Epic: Truncation test", "Test.", kind="epic")
    current = ctx.service.create("Current ticket", "desc", parent_id=epic.id)
    long_title = "A" * 100 + " suffix"
    ctx.service.create(long_title, "desc", parent_id=epic.id)

    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE, State.HUMAN_MR_APPROVAL, State.DONE):
        ctx.service.transition(current.id, st)
    current = ctx.service.get(current.id)

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    RetrospectStage().run(current, ctx)
    sc = captured.get("sibling_context", "")
    assert sc, "sibling_context should exist"
    # The long title should appear in truncated form.
    assert "AAAA" in sc
    assert "..." in sc
    assert "suffix" not in sc  # truncated away
