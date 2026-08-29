from datetime import UTC
from pathlib import Path

import pytest

from robotsix_mill.agents import freshness, obsolescence, refining
from robotsix_mill.agents.refining import RefineResult
from robotsix_mill.config import Settings
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import process_ticket
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import OBSOLESCENCE_GAP_PREFIX, RefineStage
from tests.agents.conftest import _install_refine_spy, _single

# --- approval gate tests ---


def test_refine_goes_to_human_issue_approval_when_gated(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When require_approval=true, refine transitions to human_issue_approval."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert service.get(t.id).state is State.DRAFT  # worker hasn't applied transition


def test_refine_goes_to_ready_when_autonomous(ctx, service, monkeypatch, repo_config):
    """When require_approval=false, refine transitions to ready (autonomous)."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY


async def test_human_issue_approval_pauses_chain(
    ctx, service, monkeypatch, repo_config
):
    """When require_approval=true, the worker pauses at human_issue_approval
    (no stage owns it), so the ticket is not picked up by implement."""
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )
    t = service.create("Add X", "rough idea")
    # apply refine outcome with gated settings
    from robotsix_mill.config import Settings as S

    gated = S(data_dir=str(ctx.settings.data_dir), require_approval="true")
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)
    outcome = RefineStage().run(t, gated_ctx)
    service.transition(t.id, outcome.next_state, outcome.note)

    # now the ticket is in human_issue_approval — worker should stop here
    await process_ticket(t.id, gated_ctx)

    reloaded = service.get(t.id)
    assert reloaded.state is State.HUMAN_ISSUE_APPROVAL
    # worker didn't advance past human_issue_approval
    history_states = [e.state for e in service.history(t.id)]
    assert State.READY not in history_states


def test_refine_clones_repo_and_passes_repo_dir(ctx, service, monkeypatch):
    """With a forge configured, refine clones ONCE and hands the agent
    a repo_dir (so it explores locally, not via web_fetch). Idempotent:
    an existing clone is reused, not re-cloned."""
    from robotsix_mill.vcs import git_ops

    ctx.settings.forge_remote_url = "https://example.test/repo.git"
    ctx.settings.forge_target_branch = "main"
    seen = {"clone": 0, "repo_dir": "unset"}

    def fake_clone(url, dest, branch, token, **kwargs):
        seen["clone"] += 1
        (dest / ".git").mkdir(parents=True)

    def fake_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        seen["repo_dir"] = repo_dir
        return _single("## Problem\nx\n## Scope\n- y\n")

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    t = service.create("x", "do a thing")
    RefineStage().run(t, ctx)
    repo = service.workspace(t).dir / "repo"
    assert seen["clone"] == 1
    assert seen["repo_dir"] == repo  # agent got the local clone

    # second run: clone already present -> reused, not re-cloned
    service.create
    seen["clone"] = 0
    t2 = service.get(t.id)
    RefineStage().run(t2, ctx)
    assert seen["clone"] == 0
    assert seen["repo_dir"] == repo


def test_refine_clone_failure_blocks_with_history_note(ctx, service, monkeypatch):
    """Clone failure propagates to the worker. The worker's
    _handle_stage_error classifies the error and either retries
    (transient) or blocks (fatal). The stage itself no longer catches
    CalledProcessError — the worker owns the retry/block decision."""
    import subprocess

    from robotsix_mill.vcs import git_ops

    ctx.settings.forge_remote_url = "https://example.test/repo.git"
    refine_called = []

    def boom_clone(url, dest, branch, token, **kwargs):
        raise subprocess.CalledProcessError(128, "git", stderr="no access")

    def fake_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        refine_called.append(True)
        return _single("## Problem\nx\n")

    monkeypatch.setattr(git_ops, "clone", boom_clone)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)
    t = service.create("x", "do a thing")
    with pytest.raises(subprocess.CalledProcessError):
        RefineStage().run(t, ctx)
    # Refine agent was NOT invoked — we bailed before reaching it.
    assert refine_called == []
    # No agent-authored comment.
    comments = ctx.service.list_comments(t.id)
    assert not any(c.author == "refine" for c in comments)


def test_web_fetch_confined_to_web_research_subagent():
    """Invariant lock: raw web_fetch is wired ONLY inside the
    web_research sub-agent (which summarises); no other agent exposes
    it. (web_tools.py is the definition module.)"""

    import robotsix_mill.agents as ap

    offenders = [
        f.name
        for f in Path(ap.__file__).parent.glob("*.py")
        if "make_web_fetch" in f.read_text()
        and f.name not in ("web_research.py", "web_tools.py")
    ]
    assert offenders == [], f"web_fetch leaked into: {offenders}"


def test_system_prompt_forbids_guessing_line_numbers():
    """Invariant lock: the refine agent's SYSTEM_PROMPT must forbid
    guessing line numbers or byte offsets and prescribe asking explore
    for exact locations first."""
    from robotsix_mill.agents.refining import SYSTEM_PROMPT

    sentinel = "Never guess line numbers"
    assert sentinel in SYSTEM_PROMPT, (
        f"SYSTEM_PROMPT must contain anti-guessing guidance ({sentinel!r}); "
        "found no match."
    )


def test_system_prompt_forbids_re_exploring_already_read_files():
    """Invariant lock: the refine agent's SYSTEM_PROMPT must instruct
    the agent to check its conversation history before delegating to
    `explore`, and not re-explore files it has already read this turn."""
    from robotsix_mill.agents.refining import SYSTEM_PROMPT

    sentinel = "conversation history before delegating to `explore`"
    assert sentinel in SYSTEM_PROMPT, (
        f"SYSTEM_PROMPT must instruct the agent to reuse already-read "
        f"context ({sentinel!r}); found no match."
    )


def test_strip_explore_call_directives_satisfies_consistency_guard():
    """When triage gates exploration off for a 'simple' ticket the
    explore/parallel_explore tools are dropped from the resolved set, so
    the refine SYSTEM_PROMPT's `parallel_explore(...)` call directive
    must be stripped — otherwise build_agent_from_definition's
    prompt/tool-consistency guard raises ValueError (the regression that
    blocked every 'simple' refine ticket)."""
    from robotsix_mill.agents.prompt_tool_consistency import (
        unregistered_call_directives,
    )
    from robotsix_mill.agents.refining import (
        SYSTEM_PROMPT,
        _strip_explore_call_directives,
    )

    known = {"explore", "parallel_explore", "read_file", "list_dir", "run_command"}
    resolved = {"read_file", "list_dir", "run_command"}

    # The unstripped prompt trips the guard for the absent tool.
    assert unregistered_call_directives(
        SYSTEM_PROMPT, resolved_tools=resolved, known_tools=known
    ) == {"parallel_explore"}

    stripped = _strip_explore_call_directives(
        SYSTEM_PROMPT, include_explore=False, include_parallel_explore=False
    )
    # Guard is satisfied, the call directive is gone, and unrelated
    # guidance (read_file) survives.
    assert (
        unregistered_call_directives(
            stripped, resolved_tools=resolved, known_tools=known
        )
        == set()
    )
    assert "parallel_explore(" not in stripped
    assert "read_file" in stripped


def test_strip_explore_call_directives_noop_when_enabled():
    """The needs-exploration path keeps both sub-agent tools, so the
    prompt must be returned verbatim (no accidental bullet deletion)."""
    from robotsix_mill.agents.refining import (
        SYSTEM_PROMPT,
        _strip_explore_call_directives,
    )

    assert (
        _strip_explore_call_directives(
            SYSTEM_PROMPT, include_explore=True, include_parallel_explore=True
        )
        == SYSTEM_PROMPT
    )


def test_draft_to_closed_transition_is_legal():
    """DRAFT → CLOSED is a valid transition in the state machine."""
    from robotsix_mill.core.states import State as S
    from robotsix_mill.core.states import can_transition

    assert can_transition(S.DRAFT, S.CLOSED) is True


# --- freshness gate tests ---

# A draft body long enough to pass the trivial-draft guard (≥50 chars)
# and that cites multiple file paths for freshness verification.
_FRESHNESS_BODY = (
    "The following files contain issues that need fixing:\n\n"
    "- `src/robotsix_mill/core/models.py` — missing type hints\n"
    "- `src/robotsix_mill/config.py` — undocumented settings\n"
    "- `src/robotsix_mill/stages/refine.py` — overlong method\n"
    "- `docs/nonexistent.md` — missing documentation\n"
    "- `tests/test_nonexistent.py` — missing test coverage\n"
)


def test_freshness_gate_disabled_by_default(ctx, service, monkeypatch):
    """Freshness gate is off by default — draft with missing paths
    still proceeds through refine normally."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    freshness_called = False

    def fake_freshness(*, draft, repo_dir):
        nonlocal freshness_called
        freshness_called = True
        return {"stale": True, "reason": "none of 5 cited paths exist"}

    monkeypatch.setattr(freshness, "run_freshness_check", fake_freshness)

    t = service.create("Fix multiple issues", _FRESHNESS_BODY)
    out = RefineStage().run(t, ctx)

    # Gate is disabled by default — refine proceeds normally.
    assert out.next_state is State.READY
    assert not freshness_called


def test_freshness_gate_enabled_stale_draft_all_missing(
    ctx,
    service,
    settings,
    monkeypatch,
):
    """Gate enabled, draft cites ≥3 files, none exist → DONE."""
    settings.freshness_gate_enabled = True
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    def fake_freshness(*, draft, repo_dir):
        return {"stale": True, "reason": "none of 5 cited file paths exist on HEAD"}

    monkeypatch.setattr(freshness, "run_freshness_check", fake_freshness)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return orig_refine(
            settings=settings, title=title, draft=draft, repo_dir=repo_dir
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Fix multiple issues", _FRESHNESS_BODY)
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "stale or invalid finding" in out.note
    assert "none of 5 cited file paths exist on HEAD" in out.note
    assert not refine_called


def test_freshness_gate_enabled_fresh_draft(ctx, service, settings, monkeypatch):
    """Gate enabled, draft cites files that all exist → refine proceeds."""
    settings.freshness_gate_enabled = True
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    def fake_freshness(*, draft, repo_dir):
        return {"stale": False, "reason": "5/5 cited paths verified on HEAD"}

    monkeypatch.setattr(freshness, "run_freshness_check", fake_freshness)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return orig_refine(
            settings=settings, title=title, draft=draft, repo_dir=repo_dir
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Fix multiple issues", _FRESHNESS_BODY)
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_freshness_gate_enabled_trivial_draft_skipped(
    ctx,
    service,
    settings,
    monkeypatch,
):
    """Gate enabled but draft <50 chars → freshness gate skipped."""
    settings.freshness_gate_enabled = True
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    freshness_called = False

    def fake_freshness(*, draft, repo_dir):
        nonlocal freshness_called
        freshness_called = True
        return {"stale": False, "reason": "ok"}

    monkeypatch.setattr(freshness, "run_freshness_check", fake_freshness)

    t = service.create("Short", "x")  # 1 char — below threshold
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert not freshness_called


def test_freshness_gate_failure_degrades_gracefully(
    ctx,
    service,
    settings,
    monkeypatch,
):
    """Freshness check raises → refine proceeds normally (best-effort)."""
    settings.freshness_gate_enabled = True
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    def fake_freshness(*, draft, repo_dir):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(freshness, "run_freshness_check", fake_freshness)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return orig_refine(
            settings=settings, title=title, draft=draft, repo_dir=repo_dir
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Fix multiple issues", _FRESHNESS_BODY)
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


# --- obsolescence gate tests ---

# A draft body long enough to clear the trivial-draft guard (≥50 chars).
_OBSOLESCENCE_BODY = (
    "Follow-up from the parent review: remove the `pyyaml` dependency "
    "from pyproject.toml — the migration ticket replaced it with the "
    "stdlib tomllib loader, so it is no longer used anywhere.\n"
)


def test_obsolescence_gate_disabled_by_default(ctx, service, monkeypatch):
    """Obsolescence gate is off by default — the check is never invoked
    and refine proceeds normally."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    called = False

    def fake_check(*, settings, draft_title, draft_body, repo_dir):
        nonlocal called
        called = True
        return {"obsolete": True, "reason": "already done"}

    monkeypatch.setattr(obsolescence, "run_obsolescence_check", fake_check)

    t = service.create(
        "Remove pyyaml", _OBSOLESCENCE_BODY, source=SourceKind.RETROSPECT
    )
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert not called


def test_obsolescence_gate_enabled_obsolete_draft(ctx, service, settings, monkeypatch):
    """Gate enabled, non-USER draft, check says obsolete → DONE with the
    obsolescence prefix and the refine agent is not invoked."""
    settings.obsolescence_gate_enabled = True

    def fake_check(*, settings, draft_title, draft_body, repo_dir):
        return {"obsolete": True, "reason": "pyyaml already removed on HEAD"}

    monkeypatch.setattr(obsolescence, "run_obsolescence_check", fake_check)

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create(
        "Remove pyyaml", _OBSOLESCENCE_BODY, source=SourceKind.RETROSPECT
    )
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert out.note.startswith(OBSOLESCENCE_GAP_PREFIX)
    assert "pyyaml already removed on HEAD" in out.note
    assert not refine_state["called"]


def test_obsolescence_gate_enabled_not_obsolete_proceeds(
    ctx, service, settings, monkeypatch
):
    """Gate enabled but check says not obsolete → refine proceeds."""
    settings.obsolescence_gate_enabled = True

    def fake_check(*, settings, draft_title, draft_body, repo_dir):
        return {"obsolete": False, "reason": "pyyaml still listed on HEAD"}

    monkeypatch.setattr(obsolescence, "run_obsolescence_check", fake_check)

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create(
        "Remove pyyaml", _OBSOLESCENCE_BODY, source=SourceKind.RETROSPECT
    )
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]


def test_obsolescence_gate_skips_user_source(ctx, service, settings, monkeypatch):
    """Gate enabled but a USER-sourced draft is never auto-closed — the
    check is not invoked even when it would report obsolete."""
    settings.obsolescence_gate_enabled = True

    called = False

    def fake_check(*, settings, draft_title, draft_body, repo_dir):
        nonlocal called
        called = True
        return {"obsolete": True, "reason": "already done"}

    monkeypatch.setattr(obsolescence, "run_obsolescence_check", fake_check)

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Remove pyyaml", _OBSOLESCENCE_BODY, source=SourceKind.USER)
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert not called
    assert refine_state["called"]


def test_obsolescence_gate_skips_trivial_draft(ctx, service, settings, monkeypatch):
    """Gate enabled but a draft <50 chars is skipped without invoking
    the check."""
    settings.obsolescence_gate_enabled = True

    called = False

    def fake_check(*, settings, draft_title, draft_body, repo_dir):
        nonlocal called
        called = True
        return {"obsolete": True, "reason": "already done"}

    monkeypatch.setattr(obsolescence, "run_obsolescence_check", fake_check)

    _install_refine_spy(monkeypatch)

    t = service.create("Short", "x", source=SourceKind.RETROSPECT)
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert not called


def test_obsolescence_gate_failure_degrades_gracefully(
    ctx, service, settings, monkeypatch
):
    """Obsolescence check raises → refine proceeds normally (best-effort)."""
    settings.obsolescence_gate_enabled = True

    def fake_check(*, settings, draft_title, draft_body, repo_dir):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(obsolescence, "run_obsolescence_check", fake_check)

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create(
        "Remove pyyaml", _OBSOLESCENCE_BODY, source=SourceKind.RETROSPECT
    )
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]


# --- datetime timezone-awareness round-trip tests ---


def test_ticket_roundtrip_preserves_tzinfo(service):
    """AC #1: Ticket.created_at and updated_at are timezone-aware after
    a create() + get() round-trip."""

    t = service.create("roundtrip test", "body")
    reloaded = service.get(t.id)

    assert reloaded.created_at.tzinfo is not None
    assert reloaded.created_at.tzinfo == UTC
    assert reloaded.updated_at.tzinfo is not None
    assert reloaded.updated_at.tzinfo == UTC


def test_event_roundtrip_preserves_tzinfo(service):
    """AC #2: TicketEvent.at is timezone-aware after a history() call."""

    t = service.create("event tz test", "body")
    service.transition(t.id, State.READY, "refined")
    events = service.history(t.id)

    assert len(events) >= 2  # created + refined
    for ev in events:
        assert ev.at.tzinfo is not None, f"event {ev.state} at is naive"
        assert ev.at.tzinfo == UTC


def test_aware_vs_aware_comparison_no_typeerror(service):
    """AC #4: Comparing DB-loaded datetimes against aware datetimes
    must succeed without TypeError."""
    from datetime import datetime, timedelta

    t = service.create("compare test", "body")
    service.transition(t.id, State.CLOSED, "done")

    # Re-read via list() — must support comparison against aware values.
    tickets = service.list()
    ticket = next(x for x in tickets if x.id == t.id)

    # This must not raise TypeError:
    assert ticket.updated_at >= datetime.now(UTC) - timedelta(days=30)
    assert ticket.created_at >= datetime.now(UTC) - timedelta(days=30)

    # Also test fromtimestamp path used by the dedup lookback:
    now = datetime.now(UTC)
    cutoff = datetime.fromtimestamp(now.timestamp() - 30 * 86400, tz=UTC)
    assert ticket.updated_at >= cutoff  # must not raise TypeError
    assert ticket.created_at >= cutoff


# --- refine no longer auto-injects tech-reference content ---


def test_refine_agent_does_not_inject_tech_references(monkeypatch, tmp_path):
    """Refine's system prompt must stay narrow — no auto-injected
    technology constraints. Reference docs live under
    agent_references/ and are pulled on-demand by the implement
    agent via the pointer in AGENT.md. This test guards against a
    regression that re-introduces refine-time push of those docs."""
    from robotsix_mill.agents import base as base_mod

    seen_system_prompt: list[str] = []

    def fake_build_agent(
        settings, system_prompt, tools, web_knowledge, level, **kwargs
    ):
        seen_system_prompt.append(system_prompt)

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type("R", (), {"output": _single("## Problem\nok\n")})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    refining.run_refine_agent(settings=s, title="Test", draft="draft")

    assert len(seen_system_prompt) == 1
    prompt = seen_system_prompt[0]
    assert "Technology Constraints" not in prompt
    assert "agent_references" not in prompt
    assert "TZDateTime" not in prompt
    assert "DateTime(timezone=True)" not in prompt


# --- run_command tool presence ---


def test_run_command_present_when_repo_dir_given(monkeypatch, tmp_path):
    """When repo_dir is provided, run_command is among the tools
    passed to the agent."""
    from robotsix_mill.agents import base as base_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    seen_tools: list = []

    def fake_build_agent(
        settings, system_prompt, tools, web_knowledge, level, **kwargs
    ):
        seen_tools.extend(t.__name__ for t in tools)

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type("R", (), {"output": _single("## Problem\nok\n")})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s,
        title="Test",
        draft="draft",
        repo_dir=repo,
    )

    assert result.split is False
    assert result.spec_markdown == "## Problem\nok\n"
    assert "run_command" in seen_tools
    # read_file and list_dir must also be present (not regressed)
    assert "read_file" in seen_tools
    assert "list_dir" in seen_tools
    # write_file and edit_file must NOT leak in
    assert "write_file" not in seen_tools
    assert "edit_file" not in seen_tools


def test_run_command_absent_when_repo_dir_is_none(monkeypatch, tmp_path):
    """When repo_dir is None, no fs tools at all are passed to the agent
    (including run_command)."""
    from robotsix_mill.agents import base as base_mod

    seen_tools: list = []

    def fake_build_agent(
        settings, system_prompt, tools, web_knowledge, level, **kwargs
    ):
        seen_tools.extend(t.__name__ for t in tools)

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type("R", (), {"output": _single("## Problem\nok\n")})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s,
        title="Test",
        draft="draft",
        repo_dir=None,
    )

    assert result.split is False
    assert result.spec_markdown == "## Problem\nok\n"
    # No fs tools when no repo — but Langfuse tools are always present.
    for fs_tool in ("run_command", "read_file", "list_dir", "explore"):
        assert fs_tool not in seen_tools, (
            f"{fs_tool} should not be present without repo_dir"
        )
    assert "langfuse_session_cost" in seen_tools
    assert "langfuse_session_summary" in seen_tools
    assert "langfuse_list_traces" in seen_tools
    assert "langfuse_trace_detail" in seen_tools
    # langfuse_inspect_trace is only injected when repo_dir is given
    assert "langfuse_inspect_trace" not in seen_tools


def test_langfuse_tools_present_when_repo_dir_given(tmp_path, monkeypatch):
    """When repo_dir is provided, Langfuse tools are injected into the
    agent's tool list — both the four simple closures and the
    langfuse_inspect_trace sub-agent tool."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.agents import base as _base
    from robotsix_mill.agents.refining import run_refine_agent
    from robotsix_mill.config import Secrets

    _cfg._reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="k")
    settings = Settings(data_dir=str(tmp_path))

    repo = tmp_path / "repo"
    repo.mkdir()

    captured: dict = {}

    class _FakeResult:
        output = RefineResult(spec_markdown="ok")

        def all_messages_json(self):
            return b"[]"

        def new_messages_json(self):
            return b"[]"

    class _FakeHandle:
        def run_sync(self, *a, **k):
            return _FakeResult()

        def close(self):
            pass

    monkeypatch.setattr(
        _base,
        "build_agent_from_definition",
        lambda settings, definition, *, tools=None, **kw: (
            captured.update(tools=tools or []) or _FakeHandle()
        ),
    )
    # Stub langfuse client functions so the closures don't hit the network
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid: "summary",
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params: {"data": []},
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: None,
    )

    run_refine_agent(settings=settings, title="x", draft="y", repo_dir=repo)

    names = [getattr(t, "__name__", "") for t in captured["tools"]]
    # Four simple langfuse tools always present
    assert "langfuse_session_cost" in names
    assert "langfuse_session_summary" in names
    assert "langfuse_list_traces" in names
    assert "langfuse_trace_detail" in names
    # Trace-inspect sub-agent present only when repo_dir is given
    assert "langfuse_inspect_trace" in names
    # Cost-inspect tool present only when repo_dir is given
    assert "inspect_cost" in names


def test_langfuse_inspect_trace_absent_when_repo_dir_none(tmp_path, monkeypatch):
    """When repo_dir is None, the four simple Langfuse tools are still
    injected but langfuse_inspect_trace is excluded."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.agents import base as _base
    from robotsix_mill.agents.refining import run_refine_agent
    from robotsix_mill.config import Secrets

    _cfg._reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="k")
    settings = Settings(data_dir=str(tmp_path))

    captured: dict = {}

    class _FakeResult:
        output = RefineResult(spec_markdown="ok")

        def all_messages_json(self):
            return b"[]"

        def new_messages_json(self):
            return b"[]"

    class _FakeHandle:
        def run_sync(self, *a, **k):
            return _FakeResult()

        def close(self):
            pass

    monkeypatch.setattr(
        _base,
        "build_agent_from_definition",
        lambda settings, definition, *, tools=None, **kw: (
            captured.update(tools=tools or []) or _FakeHandle()
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid: 0.0,
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_session_summary",
        lambda settings, sid: "summary",
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        lambda settings, path, params: {"data": []},
    )
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, tid: None,
    )

    run_refine_agent(settings=settings, title="x", draft="y", repo_dir=None)

    names = [getattr(t, "__name__", "") for t in captured["tools"]]
    # Four simple langfuse tools always present
    assert "langfuse_session_cost" in names
    assert "langfuse_session_summary" in names
    assert "langfuse_list_traces" in names
    assert "langfuse_trace_detail" in names
    # Trace-inspect sub-agent NOT present when repo_dir is None
    assert "langfuse_inspect_trace" not in names
    # Cost-inspect tool NOT present when repo_dir is None
    assert "inspect_cost" not in names
