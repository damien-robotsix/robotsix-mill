import pytest

from robotsix_mill.agents import retrospecting
from robotsix_mill.agents.retrospecting import RetrospectResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.text_utils import truncate_at_boundary
from robotsix_mill import langfuse_client
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.retrospect import RetrospectStage


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    s = Settings(**env)
    db.init_db(s)
    return StageContext(settings=s, service=TicketService(s))


def _done(ctx):
    t = ctx.service.create("Add X", "spec body")
    for st in (State.READY, State.DELIVERABLE, State.IN_REVIEW, State.DONE):
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
    assert drafts[0].source == "retrospect"


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
    memory_file = ctx.settings.retrospect_memory_file
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("## Issue: slow tests\n- ticket-A: 3 retries\n", encoding="utf-8")

    captured_memory = []

    def capture(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)
    RetrospectStage().run(_done(ctx), ctx)
    assert captured_memory == ["## Issue: slow tests\n- ticket-A: 3 retries\n"]


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
    memory_file = ctx.settings.retrospect_memory_file
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
    memory_file = ctx.settings.retrospect_memory_file
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
    expected = ctx.settings.data_dir / "retrospect_memory.md"
    assert ctx.settings.retrospect_memory_file == expected


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
# deep-analysis frequency gate tests
# ---------------------------------------------------------------------------


def test_deep_counter_missing_treated_as_zero(tmp_path):
    """Missing counter file → read returns 0."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    settings = Settings(MILL_DATA_DIR=str(tmp_path / "data"))
    # Ensure the counter file does not exist.
    counter_path = RetrospectStage._deep_counter_path(settings)
    assert not counter_path.exists()
    assert RetrospectStage._read_deep_counter(settings) == 0


def test_deep_counter_corrupted_resets_to_zero(tmp_path):
    """Corrupted (non-integer) counter resets to 0."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    settings = Settings(MILL_DATA_DIR=str(tmp_path / "data"))
    counter_path = RetrospectStage._deep_counter_path(settings)
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text("garbage", encoding="utf-8")
    assert RetrospectStage._read_deep_counter(settings) == 0


def test_deep_counter_normal_read_write(tmp_path):
    """Write then read returns the expected value."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    settings = Settings(MILL_DATA_DIR=str(tmp_path / "data"))
    RetrospectStage._write_deep_counter(settings, 7)
    assert RetrospectStage._read_deep_counter(settings) == 7


def test_frequency_gate_light_runs_increment(tmp_path):
    """With frequency=3: runs 1,2,3 are light (counter increments)."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    settings = Settings(
        MILL_DATA_DIR=str(tmp_path / "data"),
        MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY="3",
    )
    # Counter starts at 0.
    assert RetrospectStage._read_deep_counter(settings) == 0

    # Simulate three light runs.
    for expected_counter in (0, 1, 2):
        counter = RetrospectStage._read_deep_counter(settings)
        assert counter < 3  # still light
        RetrospectStage._write_deep_counter(settings, counter + 1)

    # After third light run, counter reaches 3.
    assert RetrospectStage._read_deep_counter(settings) == 3


def test_frequency_gate_deep_run_resets(tmp_path):
    """When counter >= frequency, deep analysis triggered and counter resets."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    settings = Settings(
        MILL_DATA_DIR=str(tmp_path / "data"),
        MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY="3",
    )
    RetrospectStage._write_deep_counter(settings, 3)
    counter = RetrospectStage._read_deep_counter(settings)
    assert counter >= 3
    # Reset for deep run.
    RetrospectStage._write_deep_counter(settings, 0)
    assert RetrospectStage._read_deep_counter(settings) == 0


def test_deep_analysis_passes_trace_ids_to_agent(tmp_path, monkeypatch):
    """In deep mode, trace_ids are passed to run_retrospect_agent."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    ctx = _ctx(tmp_path, MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY="1")
    _no_langfuse(monkeypatch)

    # Force deep analysis: set counter >= frequency
    RetrospectStage._write_deep_counter(ctx.settings, 5)

    # Mock the Langfuse trace list to return trace IDs.
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda s, path, params: {
            "data": [
                {"id": "trace-a", "name": "implement"},
                {"id": "trace-b", "name": "test"},
            ]
        },
    )

    captured_kwargs = {}

    def capture(**kwargs):
        captured_kwargs.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert captured_kwargs.get("deep_analysis") is True
    assert captured_kwargs.get("trace_ids") == ["trace-a", "trace-b"]


def test_light_analysis_unchanged(tmp_path, monkeypatch):
    """When deep_analysis=False, the retrospect agent receives no
    trace_ids and behaves exactly as today."""
    ctx = _ctx(tmp_path, MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY="10")
    _no_langfuse(monkeypatch)

    # Counter=0 → light analysis (not deep).
    RetrospectStage._write_deep_counter(ctx.settings, 0)

    captured_kwargs = {}

    def capture(**kwargs):
        captured_kwargs.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert captured_kwargs.get("deep_analysis") is False
    assert captured_kwargs.get("trace_ids") == []


def test_light_analysis_increments_counter(tmp_path, monkeypatch):
    """After a light analysis run, the counter is incremented by 1."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    ctx = _ctx(tmp_path, MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY="10")
    _no_langfuse(monkeypatch)

    RetrospectStage._write_deep_counter(ctx.settings, 3)

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(),
    )

    RetrospectStage().run(_done(ctx), ctx)
    assert RetrospectStage._read_deep_counter(ctx.settings) == 4


def test_deep_analysis_resets_counter(tmp_path, monkeypatch):
    """After a deep analysis run, the counter is reset to 0."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    ctx = _ctx(tmp_path, MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY="3")
    _no_langfuse(monkeypatch)

    # Set counter to trigger deep analysis.
    RetrospectStage._write_deep_counter(ctx.settings, 3)

    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda s, path, params: {"data": []},
    )
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _default_result(),
    )

    RetrospectStage().run(_done(ctx), ctx)
    assert RetrospectStage._read_deep_counter(ctx.settings) == 0


def test_deep_analysis_handles_missing_langfuse_traces(tmp_path, monkeypatch):
    """When Langfuse returns None for trace list in deep mode, trace_ids
    is empty and the agent still runs (degraded)."""
    from robotsix_mill.stages.retrospect import RetrospectStage

    ctx = _ctx(tmp_path, MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY="1")
    _no_langfuse(monkeypatch)

    RetrospectStage._write_deep_counter(ctx.settings, 5)

    # Langfuse trace list returns None (unconfigured / error).
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda s, path, params: None,
    )

    captured_kwargs = {}

    def capture(**kwargs):
        captured_kwargs.update(kwargs)
        return _default_result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", capture)

    t = _done(ctx)
    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert captured_kwargs.get("deep_analysis") is True
    assert captured_kwargs.get("trace_ids") == []
