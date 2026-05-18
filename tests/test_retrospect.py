import pytest

from robotsix_mill.agents import retrospecting
from robotsix_mill.agents.retrospecting import RetrospectResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
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
