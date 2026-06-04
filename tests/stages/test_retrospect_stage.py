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
from robotsix_mill.draft_target import looks_like_mill_internal
from robotsix_mill.stages.retrospect import (
    RetrospectStage,
    _WORD_TO_NUM,
    _check_memory_count_consistency,
    _extract_ticket_ids,
    _parse_numeric_count,
)


def test_looks_like_mill_internal_matches_pipeline_symbols():
    """A draft body that names multiple mill-internal symbols / paths
    triggers the safety-net override."""
    title = "Scope-triage loops indefinitely re-evaluating runtime artifact"
    body = (
        "The bug lives in `src/robotsix_mill/stages/implement.py` — the "
        "scope-triage agent doesn't dedupe files it has already "
        "classified. Fix should add a per-ticket dedup set in the "
        "stage handler and cap iterations."
    )
    assert looks_like_mill_internal(title, body) is True


def test_looks_like_mill_internal_ignores_repo_specific_fixes():
    """A draft about the audited repo's own code (no mill internals
    mentioned) stays on ``current`` — no override."""
    assert (
        looks_like_mill_internal(
            "Add docstrings to mail_box.py public methods",
            "The IMAP wrapper at `src/robotsix_auto_mail/mail_box.py` "
            "has 4 public methods missing docstrings. Add them following "
            "the existing module's style.",
        )
        is False
    )


def test_looks_like_mill_internal_requires_two_hits():
    """Single-keyword mention is insufficient (false-positive
    suppression). A passing reference to ``stages/`` alone in an
    otherwise-repo-specific body doesn't trigger the override."""
    assert (
        looks_like_mill_internal(
            "Refactor IMAP error paths in mail_box.py",
            "Pattern was copied from mill's stages/ directory but lives "
            "entirely in src/robotsix_auto_mail/mail_box.py.",
        )
        is False
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


def _ticket(
    ctx, title="Test ticket", body="Test description", branch="mill/test-branch"
):
    """Create a ticket and transition it through to State.DONE."""
    t = ctx.service.create(title, body)
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DOCUMENTING)
    ctx.service.transition(t.id, State.DELIVERABLE)
    ctx.service.transition(t.id, State.IMPLEMENT_COMPLETE)
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
        s = Settings(data_dir=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        created.append(s)
        from robotsix_mill.config import RepoConfig

        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


# ------------------------------------------------------------------
# 1. Happy path
# ------------------------------------------------------------------


def test_happy_path_normal_retrospect_closed_with_findings(ctx_factory, monkeypatch):
    """Happy path: agent returns normal findings → CLOSED with
    retrospect.md artifact written, langfuse: yes."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    # Default ALL seams
    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(findings="All good.", conclusion="done"),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "session summary text",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: None,
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", _boom)
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    """When retrospect_spawn_drafts=false, a proposed draft is
    noted but NOT created."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory(retrospect_spawn_drafts="false")

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="Fix X",
            draft_body="Do X",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="Fix X",
            draft_body="Do X",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="No notable issues — clean run",
            draft_body="Nothing",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            follow_up_title="Incomplete: add tests",
            follow_up_body="Missing coverage",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    # Pre-create a CLOSED ticket with same title
    pre = ctx.service.create("Incomplete: add tests", "Old")
    ctx.service.transition(pre.id, State.READY)
    ctx.service.transition(pre.id, State.DOCUMENTING)
    ctx.service.transition(pre.id, State.DELIVERABLE)
    ctx.service.transition(pre.id, State.IMPLEMENT_COMPLETE)
    ctx.service.transition(pre.id, State.HUMAN_MR_APPROVAL)
    ctx.service.transition(pre.id, State.DONE)
    ctx.service.transition(pre.id, State.CLOSED)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            follow_up_title="Incomplete: add tests",
            follow_up_body="Missing coverage",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    # Pre-create a DRAFT ticket with same title (case-insensitive)
    ctx.service.create("Incomplete: add tests", "Existing draft")

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            follow_up_title="Incomplete: add tests",
            follow_up_body="Missing coverage",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "follow-up" not in (out.note or "")
    # Only the original + pre-existing DRAFT
    assert len(ctx.service.list()) == 2


# (Removed) deep-analysis gate tests — deep-analysis mode was retired
# from the retrospect stage; per-trace inspection is now owned by the
# periodical cost-evaluation pipeline.


# ------------------------------------------------------------------
# 12. Memory persistence
# ------------------------------------------------------------------


def test_updated_memory_written_to_file(ctx_factory, monkeypatch):
    """Agent's updated_memory is written to the retrospect_memory_file."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    memory_content = "## Issue\nEvidence: observed in TKT-001"

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(updated_memory=memory_content),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    RetrospectStage().run(t, ctx)

    memory_file = ctx.settings.memory_file_for(
        "retrospect",
        ctx.repo_config.board_id if ctx.repo_config else "",
    )
    assert memory_file.exists()
    assert memory_file.read_text() == memory_content


def _memory_seams(monkeypatch):
    """Install the common seams for the memory-delta persistence tests
    (everything except run_retrospect_agent, which each test supplies)."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )


def test_no_change_run_skips_write(ctx_factory, monkeypatch):
    """Case 1: updated_memory="" and memory_delta=None → the memory file
    is neither created nor modified; the ticket still CLOSES."""
    ctx = ctx_factory()
    _memory_seams(monkeypatch)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(updated_memory="", memory_delta=None),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    memory_file = ctx.settings.memory_file_for(
        "retrospect",
        ctx.repo_config.board_id if ctx.repo_config else "",
    )
    assert not memory_file.exists()


def test_no_change_run_leaves_existing_file_untouched(ctx_factory, monkeypatch):
    """Case 1: when a memory file already exists and the agent makes no
    change, the stored ledger is preserved byte-for-byte."""
    ctx = ctx_factory()
    _memory_seams(monkeypatch)

    memory_file = ctx.settings.memory_file_for(
        "retrospect",
        ctx.repo_config.board_id if ctx.repo_config else "",
    )
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    original = "## Existing Pattern\n\nEvidence: observed in TKT-001\n"
    memory_file.write_text(original, encoding="utf-8")

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(updated_memory="", memory_delta=None),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert memory_file.read_text() == original


def test_append_only_run_merges_delta(ctx_factory, monkeypatch):
    """Case 2: updated_memory="" + memory_delta merges the delta onto the
    existing ledger, existing first then new, separated by a blank line."""
    ctx = ctx_factory()
    _memory_seams(monkeypatch)

    memory_file = ctx.settings.memory_file_for(
        "retrospect",
        ctx.repo_config.board_id if ctx.repo_config else "",
    )
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text(
        "## Existing Pattern\n\nEvidence: observed in TKT-001\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            updated_memory="",
            memory_delta="## New Pattern\n\nObserved in TKT-XXX.",
        ),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    content = memory_file.read_text()
    assert "## Existing Pattern" in content
    assert "## New Pattern" in content
    # Existing content comes first, then the appended delta.
    assert content.index("## Existing Pattern") < content.index("## New Pattern")
    assert "Evidence: observed in TKT-001\n\n## New Pattern" in content


def test_first_run_delta_creates_file(ctx_factory, monkeypatch):
    """Case 2 (first run): no memory file exists and only memory_delta is
    returned → the delta becomes the initial ledger content."""
    ctx = ctx_factory()
    _memory_seams(monkeypatch)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            updated_memory="",
            memory_delta="## New Pattern\n\nObserved in TKT-XXX.",
        ),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    memory_file = ctx.settings.memory_file_for(
        "retrospect",
        ctx.repo_config.board_id if ctx.repo_config else "",
    )
    assert memory_file.exists()
    assert memory_file.read_text() == "## New Pattern\n\nObserved in TKT-XXX."


def test_both_fields_updated_memory_wins(ctx_factory, monkeypatch):
    """Defensive: if the agent violates the prompt and returns BOTH a
    non-empty updated_memory AND a memory_delta, the full-rewrite path
    wins and the delta is ignored."""
    ctx = ctx_factory()
    _memory_seams(monkeypatch)

    memory_file = ctx.settings.memory_file_for(
        "retrospect",
        ctx.repo_config.board_id if ctx.repo_config else "",
    )
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("## Old\n\nstale\n", encoding="utf-8")

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            updated_memory="## Full Rewrite\n\nThe complete ledger.",
            memory_delta="## Should Be Ignored\n\nnope",
        ),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    content = memory_file.read_text()
    assert content == "## Full Rewrite\n\nThe complete ledger."
    assert "Should Be Ignored" not in content


# ------------------------------------------------------------------
# 13. Memory count consistency — drift is non-blocking
# ------------------------------------------------------------------


def test_memory_count_drift_non_blocking(ctx_factory, monkeypatch):
    """When the memory ledger has count drift (claims 5 tickets but
    evidence lists 2), the stage still transitions to CLOSED."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    drift_memory = (
        "## Bug\nClaims 5 tickets demonstrate this pattern.\n- `TKT-001`\n- `TKT-002`\n"
    )

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(updated_memory=drift_memory),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    prune_calls = []

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    # Mock prune_clone locally where it's called
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: prune_calls.append(ws),
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert len(prune_calls) == 1
    assert prune_calls[0].dir == ctx.service.workspace(t).dir


# ------------------------------------------------------------------
# 15. prune_clone_on_close=False
# ------------------------------------------------------------------


def test_prune_clone_on_close_false_no_prune(ctx_factory, monkeypatch):
    """When prune_clone_on_close=false, prune_clone is NOT called."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory(prune_clone_on_close="false")

    prune_calls = []

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: prune_calls.append(ws),
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
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
        memory = "## Bug pattern\n5 tickets show this bug.\n- `TKT-001`\n- `TKT-002`\n"
        warnings = _check_memory_count_consistency(memory)
        assert len(warnings) == 1
        assert "Bug pattern" in warnings[0]
        assert "5" in warnings[0]
        assert "2" in warnings[0]

    def test_no_claim_no_warning(self):
        memory = "## Bug pattern\n- `TKT-001`\n- `TKT-002`"
        assert _check_memory_count_consistency(memory) == []

    def test_exact_match_no_warning(self):
        memory = "## Bug pattern\n2 tickets show this bug.\n- `TKT-001`\n- `TKT-002`\n"
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


# ------------------------------------------------------------------
# 17. AGENT.md proposal writing
# ------------------------------------------------------------------


def test_agented_proposals_written_to_candidates_file(ctx_factory, monkeypatch):
    """When agent returns agented_md_proposals, they are appended to
    AGENT_CANDIDATES.md in the persistent per-board data directory
    (outside the ephemeral clone)."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            agented_md_proposals=[
                {
                    "section": "## Board UI",
                    "rule": "Always update board.js when adding new UI elements.",
                    "rationale": "Observed on T-abc, T-def, T-ghi.",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    # AGENT_CANDIDATES.md in persistent per-board data dir, NOT in the
    # ephemeral clone that prune_clone would wipe.
    s = ctx.settings
    candidates_path = s.data_dir / "test-board" / "AGENT_CANDIDATES.md"
    assert candidates_path.exists()
    content = candidates_path.read_text()
    assert "### Proposed addition to ## Board UI" in content
    assert "Always update board.js when adding new UI elements" in content
    assert "Observed on T-abc, T-def, T-ghi" in content


def test_agented_proposals_none_no_file_created(ctx_factory, monkeypatch):
    """When agented_md_proposals is None, AGENT_CANDIDATES.md is NOT created
    (or left unchanged if it existed)."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(agented_md_proposals=None),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    s = ctx.settings
    candidates_path = s.data_dir / "test-board" / "AGENT_CANDIDATES.md"
    assert not candidates_path.exists()


def test_agented_proposals_empty_list_no_file_created(ctx_factory, monkeypatch):
    """An empty list is treated the same as None — no file created."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(agented_md_proposals=[]),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    s = ctx.settings
    candidates_path = s.data_dir / "test-board" / "AGENT_CANDIDATES.md"
    assert not candidates_path.exists()


def test_agented_proposals_append_only(ctx_factory, monkeypatch):
    """When AGENT_CANDIDATES.md already exists in the persistent data dir,
    new proposals are appended without overwriting."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()

    # Pre-populate the persistent candidates file for this board.
    s = ctx.settings
    candidates_path = s.data_dir / "test-board" / "AGENT_CANDIDATES.md"
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    candidates_path.write_text(
        "### Proposed addition to ## Prior Section\n\n> **Rule:** Old rule.\n\n---\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            agented_md_proposals=[
                {
                    "section": "## Board UI",
                    "rule": "Always update board.js when adding new UI elements.",
                    "rationale": "Observed on T-abc.",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    content = candidates_path.read_text()
    # Old content is preserved
    assert "### Proposed addition to ## Prior Section" in content
    assert "Old rule" in content
    # New content is appended after
    assert "### Proposed addition to ## Board UI" in content
    assert "Always update board.js" in content


def test_agented_proposals_gated_by_setting(ctx_factory, monkeypatch):
    """When MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS=false, proposals
    are not written even if present."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory(MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS="false")

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            agented_md_proposals=[
                {
                    "section": "## Board UI",
                    "rule": "Always update board.js.",
                    "rationale": "T-abc.",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    s = ctx.settings
    candidates_path = s.data_dir / "test-board" / "AGENT_CANDIDATES.md"
    assert not candidates_path.exists()


# ------------------------------------------------------------------
# 18. AGENT.md proposal ticket filing (in addition to the candidates file)
# ------------------------------------------------------------------


def _agented_seams(monkeypatch):
    """Install the common seams used by the AGENT.md-proposal-ticket
    tests (everything except run_retrospect_agent, which each test
    supplies)."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )


def test_agented_proposals_file_tickets_on_enable(ctx_factory, monkeypatch):
    """N proposals → N draft tickets on ctx.service's board, each in
    State.DRAFT, source RETROSPECT, parent set to the originating
    ticket; each body carries its section/rule/rationale + origin id."""
    from robotsix_mill.core.models import SourceKind

    ctx = ctx_factory()
    _agented_seams(monkeypatch)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            agented_md_proposals=[
                {
                    "section": "## Board UI",
                    "rule": "Always update board.js when adding new UI.",
                    "rationale": "Observed on T-abc.",
                },
                {
                    "section": "## Git / CI",
                    "rule": "Rebase before committing.",
                    "rationale": "Main moves under you.",
                },
            ]
        ),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    spawned = [tk for tk in ctx.service.list() if tk.id != t.id]
    assert len(spawned) == 2
    for tk in spawned:
        assert tk.state is State.DRAFT
        assert tk.source == SourceKind.RETROSPECT
        assert tk.parent_id == t.id

    bodies = {tk.title: ctx.service.workspace(tk).read_description() for tk in spawned}
    joined = "\n".join(bodies.values())
    assert "## Board UI" in joined
    assert "Always update board.js when adding new UI." in joined
    assert "Observed on T-abc." in joined
    assert "## Git / CI" in joined
    assert "Rebase before committing." in joined
    # Each body references the originating ticket id.
    for body in bodies.values():
        assert t.id in body


def test_agented_proposal_tickets_gated_by_setting(ctx_factory, monkeypatch):
    """When retrospect_spawn_agented_proposals is disabled, no proposal
    tickets are filed (and no candidates file is written)."""
    ctx = ctx_factory(MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS="false")
    _agented_seams(monkeypatch)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            agented_md_proposals=[
                {
                    "section": "## Board UI",
                    "rule": "Always update board.js.",
                    "rationale": "T-abc.",
                }
            ]
        ),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    # Only the original ticket — no proposal tickets filed.
    assert [tk.id for tk in ctx.service.list()] == [t.id]


@pytest.mark.parametrize("proposals", [None, []])
def test_agented_proposal_tickets_none_or_empty_no_filing(
    ctx_factory, monkeypatch, proposals
):
    """None or an empty proposal list → no proposal tickets filed."""
    ctx = ctx_factory()
    _agented_seams(monkeypatch)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(agented_md_proposals=proposals),
    )

    t = _ticket(ctx)
    out = RetrospectStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert [tk.id for tk in ctx.service.list()] == [t.id]


def test_agented_proposal_tickets_dedup_on_repeat(ctx_factory, monkeypatch):
    """Filing is idempotent at the title level: a second run with the
    same proposal does not add a duplicate ticket."""
    ctx = ctx_factory()
    _agented_seams(monkeypatch)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            agented_md_proposals=[
                {
                    "section": "## Board UI",
                    "rule": "Always update board.js when adding new UI.",
                    "rationale": "Observed on T-abc.",
                }
            ]
        ),
    )

    t = _ticket(ctx)
    RetrospectStage().run(t, ctx)
    after_first = [tk for tk in ctx.service.list() if tk.id != t.id]
    assert len(after_first) == 1

    # Second run, same proposal → no duplicate.
    RetrospectStage().run(t, ctx)
    after_second = [tk for tk in ctx.service.list() if tk.id != t.id]
    assert len(after_second) == 1
    assert after_second[0].id == after_first[0].id


# ---------------------------------------------------------------------------
# 22. Draft routing: draft_target="mill" lands on the configured mill board
# ---------------------------------------------------------------------------


def _multirepo_ctx(tmp_path):
    """Build a StageContext with TWO registered repos: the current
    ticket's board ("test-board") and a separate "mill-board" used as
    the trace-review target. Mirrors the multi-repo deployment
    topology where retrospect must choose between two real
    destinations."""
    from robotsix_mill.config import (
        RepoConfig,
        ReposRegistry,
        Settings,
    )
    from robotsix_mill.core import db
    from robotsix_mill.core.service import TicketService
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
    s = Settings(
        data_dir=str(tmp_path),
        trace_review_target_repo_id="robotsix-mill",
    )
    db.init_db(s, board_id="test-board")
    db.init_db(s, board_id="mill-board")
    svc = TicketService(s, board_id="test-board")
    return StageContext(
        settings=s,
        service=svc,
        repo_config=_cfg._repos_config.repos["test-repo"],
    )


def test_draft_target_mill_routes_to_mill_board(tmp_path, fake_sandbox, monkeypatch):
    """When the retrospect agent returns ``draft_target="mill"`` and
    ``trace_review_target_repo_id`` resolves to a known repo, the
    draft is created on THAT repo's board — not on the originating
    ticket's board. This is the 6934-style "mill-internal pipeline
    issue surfaced during retrospect of an auto-mail ticket" path:
    the fix lives in mill source, so the ticket needs to be on the
    mill board to flow through mill's refine/implement cycle."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner
    import robotsix_mill.config as _cfg

    ctx = _multirepo_ctx(tmp_path)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="Doc agent silent failures",
            draft_body="Fix lives in stages/document.py",
            draft_target="mill",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-mill",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    try:
        t = _ticket(ctx)
        out = RetrospectStage().run(t, ctx)

        assert out.next_state is State.CLOSED

        # Originating board still has only the original ticket — the
        # draft did NOT land here.
        on_current = ctx.service.list()
        assert len(on_current) == 1
        assert on_current[0].id == t.id

        # Draft lives on the mill maintenance board.
        from robotsix_mill.core.service import TicketService

        mill_svc = TicketService(ctx.settings, board_id="mill-board")
        on_mill = mill_svc.list()
        assert len(on_mill) == 1
        assert on_mill[0].title == "Doc agent silent failures"
        # The cross-board parent link is dropped (a parent on the
        # originating board would dangle from the mill DB's view).
        assert on_mill[0].parent_id is None
    finally:
        _cfg._reset_repos_config()


# ---------------------------------------------------------------------------
# 23. Routing fallback: misconfigured "mill" target falls back to current
# ---------------------------------------------------------------------------


def test_draft_target_mill_falls_back_when_unset(ctx_factory, monkeypatch):
    """When ``draft_target="mill"`` but ``trace_review_target_repo_id``
    is unset, the helper MUST fall back to the current repo with a
    warning. A misconfigured target must never lose a draft — silent
    "draft vanished into a non-existent board" was exactly the
    failure-mode-of-the-week we're trying to prevent."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner

    ctx = ctx_factory()  # no MILL_TRACE_REVIEW_TARGET_REPO_ID

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=True,
            draft_title="Mill fix",
            draft_body="Fix lives in mill code",
            draft_target="mill",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    t = _ticket(ctx)
    RetrospectStage().run(t, ctx)

    # Fell back to the current repo: the draft IS on the originating
    # board (better than losing it).
    on_current = ctx.service.list()
    assert any(tk.title == "Mill fix" for tk in on_current)


# ---------------------------------------------------------------------------
# 24. Follow-up routing: follow_up_target="mill"
# ---------------------------------------------------------------------------


def test_follow_up_target_mill_routes_to_mill_board(
    tmp_path, fake_sandbox, monkeypatch
):
    """``follow_up_target`` follows the same routing as ``draft_target``
    — a concrete incomplete-work item on a mill-internal feature
    belongs on the mill board, not on the audited repo."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner
    import robotsix_mill.config as _cfg

    ctx = _multirepo_ctx(tmp_path)

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(
            propose_draft=False,
            follow_up_title="Wire real X in mill",
            follow_up_body="See mill source",
            follow_up_target="mill",
        ),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    try:
        t = _ticket(ctx)
        RetrospectStage().run(t, ctx)

        from robotsix_mill.core.service import TicketService

        mill_svc = TicketService(ctx.settings, board_id="mill-board")
        on_mill = mill_svc.list()
        assert any(tk.title == "Wire real X in mill" for tk in on_mill)

        on_current = ctx.service.list()
        # Original ticket only; follow-up not duplicated here.
        assert [tk.id for tk in on_current] == [t.id]
    finally:
        _cfg._reset_repos_config()


# ---------------------------------------------------------------------------
# Multi-repo defensive PR-merge verification
# ---------------------------------------------------------------------------


def _install_multirepo_registry(entries: list[tuple[str, str]]) -> None:
    """Populate the global ``_repos_config`` for multi-repo retrospect
    tests."""
    from robotsix_mill.config import RepoConfig, ReposRegistry, _reset_repos_config
    import robotsix_mill.config as _cfg

    _reset_repos_config()
    _cfg._repos_config = ReposRegistry(
        repos={
            rid: RepoConfig(
                repo_id=rid,
                board_id="meta",
                langfuse_project_name=f"p-{rid}",
                langfuse_public_key=f"pk-{rid}",
                langfuse_secret_key=f"sk-{rid}",
                forge_remote_url=url,
            )
            for rid, url in entries
        }
    )


@pytest.fixture(autouse=True)
def _reset_multirepo_registry_after_each_test():
    yield
    from robotsix_mill.config import _reset_repos_config

    _reset_repos_config()


def _multirepo_forge_env() -> dict:
    """Env required for ``get_forge(s, repo_config=rc)`` to return a real
    GitHubForge whose ``pr_status`` monkeypatch can fire — without
    these, ``get_forge`` raises a config error which retrospect's
    try/except silently swallows."""
    return {
        "FORGE_KIND": "github",
        "FORGE_REMOTE_URL": "https://github.com/o/global.git",
        "FORGE_TOKEN": "t",
    }


def _set_forge_secrets() -> None:
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(forge_token="t")


def _write_multi_pr_urls(ctx, ticket, entries: list[dict]) -> None:
    import json as _json

    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "pr_urls.json").write_text(
        _json.dumps(entries, indent=2), encoding="utf-8"
    )


def test_multi_repo_retrospect_blocks_when_any_pr_not_merged(ctx_factory, monkeypatch):
    """``pr_urls.json`` lists two PRs, one is not merged → BLOCKED. The
    retrospect agent is NOT invoked, no ``retrospect.md`` is written,
    and the ticket does not transition to CLOSED."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner
    from robotsix_mill.forge import github

    ctx = ctx_factory(**_multirepo_forge_env())
    _set_forge_secrets()

    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    agent_calls = []

    def fail_if_called(**kwargs):
        agent_calls.append(kwargs)
        return _result()

    monkeypatch.setattr(retrospecting, "run_retrospect_agent", fail_if_called)
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    def fake_pr_status(self, *, source_branch):
        rurl = self._remote_url
        if rurl == remote_a:
            return {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            }
        if rurl == remote_b:
            return {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
            }
        raise AssertionError(f"unexpected remote {rurl}")

    monkeypatch.setattr(github.GitHubForge, "pr_status", fake_pr_status)

    t = _ticket(ctx)
    _write_multi_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": "mill/x",
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": "mill/x",
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "repo-b" in (out.note or "")
    # Retrospect agent must not have been invoked.
    assert agent_calls == []
    # No retrospect.md written.
    assert not (ctx.service.workspace(t).artifacts_dir / "retrospect.md").exists()


def test_multi_repo_retrospect_closes_when_all_prs_merged(ctx_factory, monkeypatch):
    """All PRs merged → retrospect runs normally and the ticket
    transitions to CLOSED."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner
    from robotsix_mill.forge import github

    ctx = ctx_factory(**_multirepo_forge_env())
    _set_forge_secrets()

    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(findings="All good.", conclusion="done"),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    def fake_pr_status(self, *, source_branch):
        rurl = self._remote_url
        return {
            "merged": True,
            "state": "closed",
            "url": (
                "https://github.com/o/a/pull/1"
                if rurl == remote_a
                else "https://github.com/o/b/pull/2"
            ),
        }

    monkeypatch.setattr(github.GitHubForge, "pr_status", fake_pr_status)

    t = _ticket(ctx)
    _write_multi_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": "mill/x",
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": "mill/x",
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    artifact = ctx.service.workspace(t).artifacts_dir / "retrospect.md"
    assert artifact.exists()


def test_single_repo_retrospect_unchanged_when_no_pr_urls_json(
    ctx_factory, monkeypatch
):
    """When ``pr_urls.json`` is absent, retrospect makes no forge calls
    for verification — the single-repo path runs unchanged."""
    from robotsix_mill import langfuse_client
    from robotsix_mill.runners import pass_runner
    from robotsix_mill.forge import github

    ctx = ctx_factory()

    monkeypatch.setattr(
        retrospecting,
        "run_retrospect_agent",
        lambda **kwargs: _result(findings="All good.", conclusion="done"),
    )
    monkeypatch.setattr(
        langfuse_client,
        "fetch_session_summary",
        lambda settings, session_id: "summary",
    )
    monkeypatch.setattr(
        langfuse_client,
        "_langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.current_session",
        lambda: "sess-abc",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.retrospect.prune_clone",
        lambda ws: None,
    )
    monkeypatch.setattr(
        pass_runner,
        "_verify_prior_proposals",
        lambda service, settings, source_label: {},
    )

    pr_status_calls = []

    def fail_if_called(self, *, source_branch):
        pr_status_calls.append(source_branch)
        return None

    monkeypatch.setattr(github.GitHubForge, "pr_status", fail_if_called)

    t = _ticket(ctx)
    # Sanity: pr_urls.json must NOT exist.
    assert not (ctx.service.workspace(t).artifacts_dir / "pr_urls.json").exists()

    out = RetrospectStage().run(t, ctx)
    assert out.next_state is State.CLOSED
    assert pr_status_calls == []  # no forge calls for verification
    artifact = ctx.service.workspace(t).artifacts_dir / "retrospect.md"
    assert artifact.exists()
