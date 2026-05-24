"""Tests for the retrospect stage (DONE → CLOSED).

Covers: happy path, Langfuse-unconfigured, agent-failure → BLOCKED,
draft spawning + no-op filtering, follow-up dedup, deep-analysis gate,
memory persistence, count-consistency drift, prune_clone gating, and
pure-function unit tests on the helper utilities.
"""

import pytest

from robotsix_mill.agents import retrospecting
from robotsix_mill.agents.retrospecting import RetrospectResult
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.retrospect import (
    RetrospectStage,
    _WORD_TO_NUM,
    _check_memory_count_consistency,
    _extract_ticket_ids,
    _parse_numeric_count,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _result(**overrides) -> RetrospectResult:
    """Module-level factory returning a RetrospectResult with sensible
    defaults, overridable per kwarg."""
    defaults: dict = dict(
        findings="All clear.",
        conclusion="Closed — clean run.",
        propose_draft=False,
        draft_title=None,
        draft_body=None,
        updated_memory="",
        draft_gap_id=None,
        follow_up_title=None,
        follow_up_body=None,
    )
    defaults.update(overrides)
    return RetrospectResult(**defaults)


def _ticket(ctx, title="Test ticket", body="Test description", branch="mill/test-branch"):
    """Create a ticket and transition it through to State.DONE."""
    t = ctx.service.create(title, body)
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DOCUMENTING)
    ctx.service.transition(t.id, State.DELIVERABLE)
    ctx.service.transition(t.id, State.HUMAN_MR_APPROVAL)
    ctx.service.transition(t.id, State.DONE)
    if branch:
        ctx.service.set_branch(t.id, branch)
    return ctx.service.get(t.id)


# ------------------------------------------------------------------
# Fixture
# ------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    """Return a factory that creates fresh StageContexts with isolated
    settings + DB, matching test_implement.py:40-55."""
    from robotsix_mill.config import Settings

    created = []

    def make(**env):
        db.reset_engine()
        s = Settings(MILL_DATA_DIR=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s)
        svc = TicketService(s)
        created.append(s)
        return StageContext(settings=s, service=svc)

    yield make
    db.reset_engine()


# ------------------------------------------------------------------
# 1. Happy path
# ------------------------------------------------------------------


def test_happy_path_normal_retrospect_closed_with_findings(ctx_factory, monkeypatch):
    """Happy path: agent returns normal findings → CLOSED with
    retrospect.md artifact written, langfuse: yes."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runtime import tracing
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    # Default ALL seams
    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(findings="All good.", conclusion="done"),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "session summary text",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        tracing, "current_session", lambda: "sess-abc",
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "done" in (out.note or "")

    artifact = ctx.service.workspace(t).artifacts_dir / "retrospect.md"
    assert artifact.exists()
    content = artifact.read_text()
    assert "langfuse: yes" in content
    assert "All good." in content


# ------------------------------------------------------------------
# 2. Langfuse unconfigured
# ------------------------------------------------------------------


def test_langfuse_none_workflow_only_still_succeeds(ctx_factory, monkeypatch):
    """When fetch_session_summary returns None, the stage still
    transitions to CLOSED and the artifact notes 'workflow-only'."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: None,
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    artifact = ctx.service.workspace(t).artifacts_dir / "retrospect.md"
    assert "langfuse: workflow-only" in artifact.read_text()


# ------------------------------------------------------------------
# 3. Agent raises → BLOCKED
# ------------------------------------------------------------------


def test_agent_raises_blocked_resumable(ctx_factory, monkeypatch):
    """When run_retrospect_agent raises, the stage returns BLOCKED
    with a resumable note and no retrospect.md artifact."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", _boom)
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "retrospect failed" in (out.note or "").lower()
    assert "resumable" in (out.note or "").lower()

    artifact = ctx.service.workspace(t).artifacts_dir / "retrospect.md"
    assert not artifact.exists()


# ------------------------------------------------------------------
# 4. retrospect_spawn_drafts=False
# ------------------------------------------------------------------


def test_spawn_drafts_disabled_no_draft_created(ctx_factory, monkeypatch):
    """When MILL_RETROSPECT_SPAWN_DRAFTS=false, a proposed draft is
    noted but NOT created."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runtime import tracing
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory(MILL_RETROSPECT_SPAWN_DRAFTS="false")

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="Fix X",
            draft_body="Do X",
        ),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        tracing, "current_session", lambda: "sess-abc",
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "draft proposed (spawning disabled)" in (out.note or "")

    # No new ticket beyond the original
    all_tickets = ctx.service.list()
    assert len(all_tickets) == 1
    assert all_tickets[0].id == t.id


# ------------------------------------------------------------------
# 5. Spawn draft enabled + agent proposes
# ------------------------------------------------------------------


def test_spawn_draft_enabled_creates_draft_with_parent(ctx_factory, monkeypatch):
    """When spawning is enabled (default) and the agent proposes
    a draft, a new ticket is created with parent_id set."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runtime import tracing
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="Fix X",
            draft_body="Do X",
        ),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        tracing, "current_session", lambda: "sess-abc",
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "improvement draft" in (out.note or "")

    all_tickets = ctx.service.list()
    # original + spawned draft = 2
    assert len(all_tickets) == 2
    spawned = [tk for tk in all_tickets if tk.id != t.id][0]
    assert spawned.title == "Fix X"
    assert spawned.parent_id == t.id
    assert spawned.id in (out.note or "")


# ------------------------------------------------------------------
# 6. No-op draft filtering
# ------------------------------------------------------------------


def test_noop_draft_title_skips_spawn(ctx_factory, monkeypatch):
    """A draft titled 'No notable issues - clean run' is filtered
    and no ticket is created."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runtime import tracing
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="No notable issues — clean run",
            draft_body="Nothing",
        ),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        tracing, "current_session", lambda: "sess-abc",
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "improvement draft" not in (out.note or "")
    # Only the original ticket
    assert len(ctx.service.list()) == 1


# ------------------------------------------------------------------
# 7. Follow-up ticket
# ------------------------------------------------------------------


def test_follow_up_ticket_created(ctx_factory, monkeypatch):
    """When agent returns follow_up_title + follow_up_body,
    a concrete follow-up ticket is created with parent_id."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runtime import tracing
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(
            follow_up_title="Incomplete: add tests",
            follow_up_body="Missing coverage",
        ),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        tracing, "current_session", lambda: "sess-abc",
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "follow-up" in (out.note or "")

    all_tickets = ctx.service.list()
    assert len(all_tickets) == 2
    spawned = [tk for tk in all_tickets if tk.id != t.id][0]
    assert spawned.title == "Incomplete: add tests"
    assert spawned.parent_id == t.id


# ------------------------------------------------------------------
# 8. Follow-up dedup — CLOSED (allowed)
# ------------------------------------------------------------------


def test_follow_up_dedup_closed_allowed(ctx_factory, monkeypatch):
    """A follow-up is created even when a CLOSED ticket with the
    same title exists (CLOSED is in _DONE_WITH)."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runtime import tracing
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    # Pre-create a CLOSED ticket with same title
    pre = ctx.service.create("Incomplete: add tests", "Old")
    ctx.service.transition(pre.id, State.READY)
    ctx.service.transition(pre.id, State.DOCUMENTING)
    ctx.service.transition(pre.id, State.DELIVERABLE)
    ctx.service.transition(pre.id, State.HUMAN_MR_APPROVAL)
    ctx.service.transition(pre.id, State.DONE)
    ctx.service.transition(pre.id, State.CLOSED)

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(
            follow_up_title="Incomplete: add tests",
            follow_up_body="Missing coverage",
        ),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        tracing, "current_session", lambda: "sess-abc",
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "follow-up" in (out.note or "")
    # original + pre-existing CLOSED + new follow-up = 3
    assert len(ctx.service.list()) == 3


# ------------------------------------------------------------------
# 9. Follow-up dedup — DRAFT (blocked)
# ------------------------------------------------------------------


def test_follow_up_dedup_draft_blocked(ctx_factory, monkeypatch):
    """A follow-up is NOT created when a DRAFT ticket with the same
    case-insensitive title already exists (DRAFT not in _DONE_WITH)."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runtime import tracing
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    # Pre-create a DRAFT ticket with same title (case-insensitive)
    ctx.service.create("Incomplete: add tests", "Existing draft")

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(
            follow_up_title="Incomplete: add tests",
            follow_up_body="Missing coverage",
        ),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        tracing, "current_session", lambda: "sess-abc",
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "follow-up" not in (out.note or "")
    # Only the original + pre-existing DRAFT
    assert len(ctx.service.list()) == 2


# ------------------------------------------------------------------
# 10. Deep analysis gate — below frequency
# ------------------------------------------------------------------


def test_deep_analysis_below_frequency_no_api_call(ctx_factory, monkeypatch):
    """When counter < frequency, deep_analysis is False and
    _langfuse_api_get is NOT called."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    agent_kwargs = {}

    def _capture_agent(**kwargs):
        agent_kwargs.update(kwargs)
        return _result()

    write_calls = []

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", _capture_agent)
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    langfuse_api_called = []

    def _fake_api_get(settings, path, params=None):
        langfuse_api_called.append(True)
        return None

    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", _fake_api_get)
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 2,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: write_calls.append(value),
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert agent_kwargs["deep_analysis"] is False
    assert agent_kwargs.get("trace_ids") == []
    # Counter incremented: 2 → 3
    assert write_calls == [3]
    # _langfuse_api_get NOT called
    assert len(langfuse_api_called) == 0


# ------------------------------------------------------------------
# 11. Deep analysis gate — at/above frequency
# ------------------------------------------------------------------


def test_deep_analysis_at_frequency_triggers_api_call(ctx_factory, monkeypatch):
    """When counter >= frequency, deep_analysis=True, counter resets
    to 0, and _langfuse_api_get is called."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    agent_kwargs = {}

    def _capture_agent(**kwargs):
        agent_kwargs.update(kwargs)
        return _result()

    write_calls = []
    langfuse_api_called = []

    def _fake_api_get(settings, path, params=None):
        langfuse_api_called.append((path, params))
        return {"data": [{"id": "trace-1"}, {"id": "trace-2"}]}

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", _capture_agent)
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", _fake_api_get)
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 10,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: write_calls.append(value),
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert agent_kwargs["deep_analysis"] is True
    assert agent_kwargs.get("trace_ids") == ["trace-1", "trace-2"]
    # Counter reset to 0
    assert write_calls == [0]
    # _langfuse_api_get was called
    assert len(langfuse_api_called) == 1


# ------------------------------------------------------------------
# 12. Memory persistence
# ------------------------------------------------------------------


def test_updated_memory_written_to_file(ctx_factory, monkeypatch):
    """Agent's updated_memory is written to the retrospect_memory_file."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    memory_content = "## Issue\nEvidence: observed in TKT-001"

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(updated_memory=memory_content),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    RetrospectStage().run(t, ctx)

    memory_file = ctx.settings.retrospect_memory_file
    assert memory_file.exists()
    assert memory_file.read_text() == memory_content


# ------------------------------------------------------------------
# 13. Memory count consistency — drift is non-blocking
# ------------------------------------------------------------------


def test_memory_count_drift_non_blocking(ctx_factory, monkeypatch):
    """When the memory ledger has count drift (claims 5 tickets but
    evidence lists 2), the stage still transitions to CLOSED."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill.core import workspace as workspace_module

    ctx = ctx_factory()

    drift_memory = (
        "## Bug\n"
        "Claims 5 tickets demonstrate this pattern.\n"
        "- `TKT-001`\n"
        "- `TKT-002`\n"
    )

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(updated_memory=drift_memory),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        workspace_module, "prune_clone", lambda ws: None,
    )
    from robotsix_mill import pass_runner
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED


# ------------------------------------------------------------------
# 14. prune_clone_on_close=True
# ------------------------------------------------------------------


def test_prune_clone_on_close_true_prunes(ctx_factory, monkeypatch):
    """When prune_clone_on_close is True (default), prune_clone is called."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory()

    prune_calls = []

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    # Mock prune_clone locally where it's called
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: prune_calls.append(ws),
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert len(prune_calls) == 1


# ------------------------------------------------------------------
# 15. prune_clone_on_close=False
# ------------------------------------------------------------------


def test_prune_clone_on_close_false_no_prune(ctx_factory, monkeypatch):
    """When MILL_PRUNE_CLONE_ON_CLOSE=false, prune_clone is NOT called."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.stages import retrospect as retrospect_module
    from robotsix_mill import pass_runner

    ctx = ctx_factory(MILL_PRUNE_CLONE_ON_CLOSE="false")

    prune_calls = []

    monkeypatch.setattr(
        retrospecting, "run_retrospect_agent",
        lambda **kwargs: _result(),
    )
    monkeypatch.setattr(
        langfuse_client, "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client, "_langfuse_api_get",
        lambda settings, path, params=None: None,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_read_deep_counter", lambda settings: 0,
    )
    monkeypatch.setattr(
        retrospect_module.RetrospectStage,
        "_write_deep_counter", lambda settings, value: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: prune_calls.append(ws),
    )
    monkeypatch.setattr(
        pass_runner, "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert len(prune_calls) == 0


# ------------------------------------------------------------------
# 16. Pure-function tests
# ------------------------------------------------------------------


def test_word_to_num_lookup():
    """_WORD_TO_NUM contains expected entries."""
    assert _WORD_TO_NUM["eleven"] == 11
    assert _WORD_TO_NUM["ninety-nine"] == 99
    assert _WORD_TO_NUM["one"] == 1
    assert _WORD_TO_NUM["twenty"] == 20


class TestParseNumericCount:
    def test_digit_count(self):
        assert _parse_numeric_count("3 tickets found") == 3

    def test_word_count(self):
        assert _parse_numeric_count("Eleven tickets demonstrate") == 11

    def test_no_claim(self):
        assert _parse_numeric_count("no claim here") is None

    def test_ticket_singular_also_matched(self):
        """'1 ticket' (singular) also matches — the regex ``tickets?``
        makes the trailing 's' optional."""
        assert _parse_numeric_count("1 ticket found") == 1


class TestExtractTicketIds:
    def test_backtick_ids(self):
        text = "- `TKT-001`\n- `TKT-002`"
        assert _extract_ticket_ids(text) == {"TKT-001", "TKT-002"}

    def test_bare_bullet_ids(self):
        text = "- TKT-003: some note\n- TKT-004: another"
        assert _extract_ticket_ids(text) == {"TKT-003", "TKT-004"}

    def test_mixed_format(self):
        text = "- `TKT-001`\n- TKT-002: note"
        assert _extract_ticket_ids(text) == {"TKT-001", "TKT-002"}

    def test_empty_text(self):
        assert _extract_ticket_ids("") == set()


class TestCheckMemoryCountConsistency:
    def test_drift_detected(self):
        memory = (
            "## Bug pattern\n"
            "5 tickets show this bug.\n"
            "- `TKT-001`\n"
            "- `TKT-002`\n"
        )
        warnings = _check_memory_count_consistency(memory)
        assert len(warnings) == 1
        assert "Bug pattern" in warnings[0]
        assert "5" in warnings[0]
        assert "2" in warnings[0]

    def test_no_claim_no_warning(self):
        memory = "## Bug pattern\n- `TKT-001`\n- `TKT-002`"
        assert _check_memory_count_consistency(memory) == []

    def test_exact_match_no_warning(self):
        memory = (
            "## Bug pattern\n"
            "2 tickets show this bug.\n"
            "- `TKT-001`\n"
            "- `TKT-002`\n"
        )
        assert _check_memory_count_consistency(memory) == []

    def test_empty_memory(self):
        assert _check_memory_count_consistency("") == []

    def test_multiple_sections(self):
        memory = (
            "## Issue A\n"
            "3 tickets\n"
            "- `T-1`\n"
            "- `T-2`\n"
            "\n"
            "## Issue B\n"
            "1 ticket\n"  # singular matches ("tickets?"), count=1 == 1 evidence → no warning
            "- `T-3`\n"
        )
        warnings = _check_memory_count_consistency(memory)
        # Issue A: claims 3, has 2 → 1 warning
        # Issue B: no plural claim → no warning
        assert len(warnings) == 1
        assert "Issue A" in warnings[0]


def test_is_noop_draft():
    """_is_noop_draft delegates to is_noop_report — title-only."""
    from robotsix_mill.stages.retrospect import _is_noop_draft

    assert _is_noop_draft("No notable issues — clean run") is True
    assert _is_noop_draft("Real ticket fixing a bug") is False
    assert _is_noop_draft("Clean run — nothing to report") is True
    assert _is_noop_draft(None) is True
    assert _is_noop_draft("") is True
