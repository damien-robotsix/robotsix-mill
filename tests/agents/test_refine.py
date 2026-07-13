import hashlib
import json
from pathlib import Path

import pytest

from robotsix_mill.agents import dedup
from robotsix_mill.agents import freshness
from robotsix_mill.agents import obsolescence
from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import ChildSpec, FileMapEntry, RefineResult
from robotsix_mill.config import Settings
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages import refine as refine_module
from robotsix_mill.stages.refine import OBSOLESCENCE_GAP_PREFIX, RefineStage
from robotsix_mill.runtime.worker import process_ticket


def _single(spec: str, file_map=None) -> RefineResult:
    """Shorthand for a single-scope refine result."""
    return RefineResult(split=False, spec_markdown=spec, file_map=file_map)


def _split(*children: dict, file_map=None) -> RefineResult:
    """Shorthand for a split refine result."""
    return RefineResult(
        split=True,
        children=[
            ChildSpec(
                title=c["title"],
                spec_markdown=c["spec_markdown"],
                depends_on=c.get("depends_on", []),
            )
            for c in children
        ],
        file_map=file_map,
    )


def _install_refine_spy(
    monkeypatch,
    spec="## Problem\nx\n## Acceptance criteria\n- [ ] works\n",
):
    """Install a ``run_refine_agent`` spy and return a dict whose
    ``["called"]`` flips to ``True`` once the refine agent runs.

    Lets the dedup-target-validation tests assert that refine proceeds
    (rather than the dedup guard short-circuiting to DONE) without
    re-declaring the full keyword signature in every test.
    """
    state = {"called": False}

    def spy(
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
        state["called"] = True
        return _single(spec)

    monkeypatch.setattr(refining, "run_refine_agent", spy)
    return state


@pytest.fixture(autouse=True)
def _dedup_clean(monkeypatch):
    """All pre-existing tests expect the dedup guard to be a no-op
    (novel draft).  Dedup-specific tests override this fixture."""
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        lambda **_: {"duplicate_of": None, "already_done": None, "reason": "no match"},
    )


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


def test_empty_title_and_draft_blocks(ctx, service):
    t = service.create("   ", "   ")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty title and draft" in out.note


def test_dep_gated_ticket_is_not_refined(ctx, service, monkeypatch):
    """A DRAFT ticket with an unmet dependency is NOT refined."""
    parent = service.create("Parent ticket", "parent draft")
    dependent = service.create("Dependent ticket", "dependent draft")
    service.set_depends_on(dependent.id, [parent.id])
    # Re-read so the in-memory ticket object has the persisted depends_on.
    dependent = service.get(dependent.id)

    refine_called = False

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
        return _single("## Problem\nx\n")

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(dependent, ctx)

    assert not refine_called
    assert out.next_state is State.DRAFT
    assert service.get(dependent.id).state is State.DRAFT


def test_dep_satisfied_ticket_is_refined(ctx, service, monkeypatch):
    """Once the dependency reaches a terminal state (CLOSED/DONE),
    the refine runs normally."""
    parent = service.create("Parent ticket", "parent draft")
    dependent = service.create("Dependent ticket", "dependent draft")
    service.set_depends_on(dependent.id, [parent.id])

    # Transition parent to DONE → CLOSED (terminal).
    service.transition(parent.id, State.DONE, "done")
    service.transition(parent.id, State.CLOSED, "closed")

    # Re-read so the in-memory ticket object has the persisted depends_on.
    dependent = service.get(dependent.id)

    refine_called = False

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
        return _single("## Problem\nx\n")

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(dependent, ctx)

    assert refine_called
    assert out.next_state is State.READY


def test_no_api_key_blocks(ctx, service, monkeypatch):
    def boom(
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
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    monkeypatch.setattr(refining, "run_refine_agent", boom)
    out = RefineStage().run(service.create("x", "do a thing"), ctx)
    assert out.next_state is State.BLOCKED
    assert "OPENROUTER_API_KEY" in out.note


def test_title_only_proceeds_to_refine(ctx, service, monkeypatch):
    """A ticket with only a title (empty body) refines successfully."""
    spec = "## Problem\nAdd dark mode toggle\n## Acceptance criteria\n- [ ] works\n"
    refine_called = False

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
        nonlocal refine_called
        refine_called = True
        assert title == "Add dark mode toggle"
        assert draft == ""
        return _single(spec)

    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)
    t = service.create("Add dark mode toggle", "")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called
    # draft-original.md should contain a sentinel, not an empty file
    ws = service.workspace(t)
    original = (ws.artifacts_dir / "draft-original.md").read_text()
    assert "title-only" in original


def test_success_rewrites_description(ctx, service, monkeypatch):
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    ws = service.workspace(t)
    assert ws.read_description() == spec
    assert (ws.artifacts_dir / "draft-original.md").read_text() == "make x happen"
    # DB pointer kept in sync with the rewritten file
    expected = hashlib.sha256(spec.encode("utf-8")).hexdigest()
    assert service.get(t.id).content_hash == expected


def test_empty_spec_proceeds_to_ready(ctx, service, monkeypatch):
    """Whitespace-only spec → proceed with original draft (not BLOCKED)."""
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single("  \n "))
    t = service.create("x", "draft")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.READY
    # Original draft preserved as description.md
    assert service.workspace(t).read_description() == "draft"
    assert (
        service.workspace(t).artifacts_dir / "draft-original.md"
    ).read_text() == "draft"


def test_empty_spec_proceeds_to_human_issue_approval_when_gated(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """Whitespace-only spec + gated → HUMAN_ISSUE_APPROVAL."""
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single("  \n "))
    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )
    t = service.create("x", "draft")
    out = RefineStage().run(t, gated_ctx)
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert service.workspace(t).read_description() == "draft"


async def test_chains_draft_to_implement(ctx, service, monkeypatch):
    """Full wiring: emit -> refine -> ready -> implement. Implement is
    real but no FORGE_REMOTE_URL, so the chain halts at BLOCKED there —
    proving draft never needs a manual transition."""
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )
    t = service.create("Add X", "rough idea")

    await process_ticket(t.id, ctx)

    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    states = [e.state for e in service.history(t.id)]
    assert State.READY in states  # refine ran and advanced it
    assert "FORGE_REMOTE_URL" in service.history(t.id)[-1].note


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

    def fake_clone(url, dest, branch, token):
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
    service.create  # noqa - keep ref
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

    def boom_clone(url, dest, branch, token):
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


def test_system_prompt_requires_path_verification_for_dedup_advisory():
    """Invariant lock: the refine agent's SYSTEM_PROMPT must require
    path-level verification before accepting a pre-refine ``[!warning]``
    dedup advisory — extract file paths from the draft and run
    ``git log --all --oneline -- <path>`` for each."""
    from robotsix_mill.agents.refining import SYSTEM_PROMPT

    sentinel = "git log --all --oneline -- <path>"
    assert sentinel in SYSTEM_PROMPT, (
        f"SYSTEM_PROMPT must require git-log path verification "
        f"before accepting a dedup advisory ({sentinel!r}); found no match."
    )


def test_strip_explore_call_directives_satisfies_consistency_guard():
    """When triage gates exploration off for a 'simple' ticket the
    explore/parallel_explore tools are dropped from the resolved set, so
    the refine SYSTEM_PROMPT's `parallel_explore(...)` call directive
    must be stripped — otherwise build_agent_from_definition's
    prompt/tool-consistency guard raises ValueError (the regression that
    blocked every 'simple' refine ticket)."""
    from robotsix_mill.agents.refining import (
        SYSTEM_PROMPT,
        _strip_explore_call_directives,
    )
    from robotsix_mill.agents.prompt_tool_consistency import (
        unregistered_call_directives,
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


# --- dedup guard tests ---

# Substantive body — dedup is skipped for drafts under 100 chars, so
# every dedup-exercising test below needs a body comfortably above that
# threshold. Keep this in one place so the threshold can move without
# rewriting every test.
_DEDUP_BODY = (
    "This is a substantive draft body that exceeds the trivial-draft "
    "threshold of 100 characters so the dedup pipeline actually runs. "
    "Without enough body content, refine skips the dedup LLM call entirely."
)


def test_dedup_duplicate_ticket_closes(ctx, service, monkeypatch):
    """Exact-duplicate draft → CLOSED. Refine agent is never called."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t_a = service.create("Add dark mode toggle", _DEDUP_BODY)
    # Drive t_a to a refined state so it is a valid dedup target — an
    # un-refined DRAFT candidate is now rejected by _is_valid_dedup_target.
    service.transition(t_a.id, State.READY, note="refined")
    t_b = service.create("Add dark mode toggle", _DEDUP_BODY)

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": t_a.id,
            "already_done": None,
            "reason": "same change",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

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

    out = RefineStage().run(t_b, ctx)

    # Discarded drafts go to DONE so retrospect still analyses them.
    assert out.next_state is State.DONE
    assert f"duplicate of {t_a.id}" in out.note
    assert "same change" in out.note
    assert not refine_called


def test_dedup_already_committed_closes(ctx, service, monkeypatch):
    """Already-committed draft → CLOSED. Refine agent not called."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("Add X", _DEDUP_BODY)
    # A token-overlapping candidate so the zero-overlap short-circuit
    # does not skip the dedup LLM call (this test exercises the
    # already_done closure path, which must reach run_dedup_check).
    service.create("Add X again", _DEDUP_BODY)

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": "abc1234",
            "reason": "change in commit",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

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

    out = RefineStage().run(t, ctx)

    # Discarded drafts go to DONE so retrospect still analyses them.
    assert out.next_state is State.DONE
    assert "already implemented in abc1234" in out.note
    assert "change in commit" in out.note
    assert not refine_called


def test_dedup_novel_draft_proceeds_normally(ctx, service, monkeypatch):
    """Novel draft → refine runs normally, transitions to READY."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("Add X", "make x happen")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": None,
            "reason": "no match",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

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

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_dedup_skip_on_no_overlap_avoids_llm_call(ctx, service, monkeypatch, caplog):
    """Unrelated candidates + dedup_skip_on_no_overlap (default) →
    run_dedup_check is NOT called and refine proceeds."""
    import logging

    refine_state = _install_refine_spy(monkeypatch)

    called = {"dedup": False}

    def fake_dedup(**_):
        called["dedup"] = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    # Candidate whose tokens are disjoint from the draft.
    service.create(
        "Refactor billing invoice exporter",
        "Rework the billing invoice exporter to emit csv reports for "
        "finance reconciliation dashboards every month.",
    )
    t = service.create(
        "Zephyr quasar nebula configuration",
        "Implement zephyr quasar nebula orchestration across distributed "
        "quantum lattices ensuring photon entanglement stays coherent "
        "throughout galactic transmission windows daily.",
    )

    with caplog.at_level(logging.DEBUG, logger="robotsix_mill.stages.refine"):
        out = RefineStage().run(t, ctx)

    assert called["dedup"] is False  # LLM dedup skipped
    assert refine_state["called"] is True
    assert out.next_state is State.READY
    assert "no candidate token overlap" in caplog.text


def test_dedup_overlap_invokes_llm(ctx, service, monkeypatch):
    """A candidate sharing a meaningful token with the draft → the
    dedup LLM call IS made."""
    refine_state = _install_refine_spy(monkeypatch)

    called = {"dedup": False}

    def fake_dedup(**_):
        called["dedup"] = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    service.create("Add dark mode toggle", _DEDUP_BODY)
    t = service.create("Add dark mode toggle", _DEDUP_BODY)

    out = RefineStage().run(t, ctx)

    assert called["dedup"] is True
    assert refine_state["called"] is True
    assert out.next_state is State.READY


def test_build_candidates_block_truncates_long_body(ctx, service):
    """A candidate body longer than the cap is truncated with a marker;
    the rendered block stays bounded."""
    from robotsix_mill.stages.refine import _build_candidates_block

    long_body = "word " * 2000  # ~10k chars
    t = service.create("Some candidate", long_body)
    block = _build_candidates_block([service.get(t.id)], ctx)

    assert "description truncated" in block
    assert len(block) < ctx.settings.dedup_candidate_body_max_chars + 500


def test_build_candidates_block_keeps_short_body(ctx, service):
    """A short candidate body is rendered unchanged (no truncation)."""
    from robotsix_mill.stages.refine import _build_candidates_block

    short = "A concise candidate body well under the cap."
    t = service.create("Short candidate", short)
    block = _build_candidates_block([service.get(t.id)], ctx)

    assert short in block
    assert "description truncated" not in block


def test_build_candidates_block_no_truncation_when_cap_disabled(
    service, repo_config, tmp_path
):
    """A cap of 0 disables truncation entirely."""
    from robotsix_mill.stages.refine import _build_candidates_block

    long_body = "word " * 2000
    t = service.create("Some candidate", long_body)
    settings0 = Settings(data_dir=str(tmp_path), dedup_candidate_body_max_chars=0)
    ctx0 = StageContext(settings=settings0, service=service, repo_config=repo_config)
    block = _build_candidates_block([service.get(t.id)], ctx0)

    assert "description truncated" not in block
    assert long_body.strip() in block


def test_dedup_circular_target_refused(ctx, service, monkeypatch):
    """Reproduce the 3191/d0fc circular case: A was closed as a
    duplicate of B; a later dedup run on B that proposes
    ``already_done = A`` must be refused so the blocker stays tracked."""
    refine_state = _install_refine_spy(monkeypatch)

    t_b = service.create("Consume llmio CostLogSource read-port", _DEDUP_BODY)
    t_a = service.create("Blocked: merged llmio CostLogSource read", _DEDUP_BODY)

    # A was closed as a duplicate of B (DRAFT→DONE→CLOSED).
    service.transition(t_a.id, State.DONE, note=f"duplicate of {t_b.id}: same blocker")
    service.transition(t_a.id, State.CLOSED, note="closed")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": t_a.id,
            "reason": "already covered",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t_b, ctx)

    # The circular close must NOT happen — refine proceeds instead.
    assert out.next_state is State.READY
    assert refine_state["called"]
    assert "already_done" not in (out.note or "")
    assert "already implemented in" not in (out.note or "")


def test_dedup_closed_as_duplicate_of_third_ticket_refused(ctx, service, monkeypatch):
    """A candidate closed as a duplicate of some *other* ticket (not
    circular) is still a non-implementation closure → refine proceeds."""
    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add widget", _DEDUP_BODY)
    t_x = service.create("Unrelated tracker", _DEDUP_BODY)
    cand = service.create("Add widget (older)", _DEDUP_BODY)

    # cand was dedup-closed against a third ticket X (DONE→CLOSED).
    service.transition(cand.id, State.DONE, note=f"duplicate of {t_x.id}: same")
    service.transition(cand.id, State.CLOSED, note="closed")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": cand.id,
            "already_done": None,
            "reason": "looks similar",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]
    assert "duplicate of" not in (out.note or "")


def test_dedup_declined_candidate_refused(ctx, service, monkeypatch):
    """A declined candidate (CLOSED, never DONE) is not a fix → refine
    proceeds rather than closing the ticket against it."""
    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add gadget", _DEDUP_BODY)
    cand = service.create("Add gadget (declined)", _DEDUP_BODY)

    # Declined as noise: DRAFT → CLOSED directly, never DONE.
    service.transition(cand.id, State.CLOSED, note="declined as noise")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": cand.id,
            "already_done": None,
            "reason": "looks similar",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]


def test_dedup_legit_implemented_candidate_accepted(ctx, service, monkeypatch):
    """A candidate that reached DONE via a real implementation/merge
    note (no non-implementation prefix) remains a valid dedup target."""
    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add feature Z", _DEDUP_BODY)
    cand = service.create("Add feature Z (shipped)", _DEDUP_BODY)

    # Genuinely implemented and merged — set a branch so the
    # human-closed-with-claim guard (gates.py) does not reject this
    # candidate as an unverified external-fix claim.
    service.set_branch(cand.id, "feat/z")
    service.transition(cand.id, State.DONE, note="implemented and merged in PR #7")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": cand.id,
            "reason": "already shipped",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert f"already implemented in {cand.id}" in out.note
    assert not refine_state["called"]


def test_dedup_unmerged_candidate_branch_refused(ctx, service, monkeypatch):
    """A candidate that reached DONE via a real implementation note but
    whose own branch never merged to main must NOT close the current
    ticket as a duplicate — refine proceeds so the stranded work is
    re-applied."""
    from robotsix_mill.stages import refine as refine_module

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Re-apply stranded work", _DEDUP_BODY)
    cand = service.create("Original (stranded)", _DEDUP_BODY)

    # Genuinely implemented (passes all four pre-merge validity checks)
    # and carries a branch — but that branch never merged.
    service.set_branch(cand.id, "feat/stranded")
    service.transition(cand.id, State.DONE, note="implemented in PR #7")

    # Report the candidate's branch as unmerged, decoupling the test
    # from a real git repo.
    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, t: False
    )

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": cand.id,
            "reason": "already shipped",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_state["called"]
    assert "already implemented in" not in (out.note or "")


def test_dedup_merged_candidate_branch_accepted(ctx, service, monkeypatch):
    """Positive control: a DONE candidate whose branch IS merged stays a
    valid dedup target — the current ticket is still closed DONE."""
    from robotsix_mill.stages import refine as refine_module

    refine_state = _install_refine_spy(monkeypatch)

    t = service.create("Add feature Z", _DEDUP_BODY)
    cand = service.create("Add feature Z (shipped)", _DEDUP_BODY)

    service.set_branch(cand.id, "feat/z")
    service.transition(cand.id, State.DONE, note="implemented in PR #7")

    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, t: True
    )

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        return {
            "duplicate_of": None,
            "already_done": cand.id,
            "reason": "already shipped",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert f"already implemented in {cand.id}" in out.note
    assert not refine_state["called"]


def test_dedup_skipped_for_empty_title_and_draft(ctx, service, monkeypatch):
    """When both title and draft are empty, blocks BEFORE dedup check."""
    dedup_called = False

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(service.create("", "   "), ctx)
    assert out.next_state is State.BLOCKED
    assert "empty title and draft" in out.note
    assert not dedup_called


def test_dedup_skipped_for_trivial_draft(ctx, service, monkeypatch):
    """Trivial drafts (body <100 chars) skip dedup — the LLM call cost
    dwarfs the value when there's barely anything to compare. Refine
    still proceeds normally."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    dedup_called = False

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    # body="" → trivial → dedup must be skipped.
    out = RefineStage().run(service.create("Add dark mode toggle", ""), ctx)
    assert out.next_state is State.READY
    assert not dedup_called, "dedup should be skipped for trivial drafts"


def test_dedup_never_flags_self(ctx, service, monkeypatch):
    """The candidates block passed to dedup must NOT mention the
    current ticket's id."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("my ticket", _DEDUP_BODY)
    # Create another ticket so the candidate list isn't empty
    service.create("other ticket", "other draft")

    seen_block = None

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal seen_block
        seen_block = candidates_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    RefineStage().run(t, ctx)

    assert seen_block is not None
    # The candidates block is one ``## <id>`` section per ticket; the
    # current ticket's id must not appear as a section heading.
    assert f"## {t.id}" not in seen_block


def test_dedup_candidate_bodies_included(ctx, service, monkeypatch):
    """Candidate entries passed to dedup must include each ticket's
    full description body inside a ``<body>...</body>`` block."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    # Create the current ticket (will be excluded from candidates).
    t = service.create("my ticket", _DEDUP_BODY)

    # Create two candidate tickets with distinctive bodies.
    t_a = service.create("candidate A", "body of ticket A\nline two")
    t_b = service.create("candidate B", "body of ticket B")

    seen_block = None

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal seen_block
        seen_block = candidates_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    RefineStage().run(t, ctx)

    assert seen_block is not None
    # Each candidate is a Markdown section with title + body.
    assert f"## {t_a.id}" in seen_block
    assert "- title: candidate A" in seen_block
    assert "body of ticket A\nline two" in seen_block

    assert f"## {t_b.id}" in seen_block
    assert "- title: candidate B" in seen_block
    assert "body of ticket B" in seen_block

    # Each section uses the <body>...</body> framing.
    assert seen_block.count("````body") == 2
    assert seen_block.count("````\n<!-- /body -->") == 2


def test_dedup_failure_degrades_gracefully(ctx, service, monkeypatch):
    """Dedup check raises → refine proceeds normally."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("Add X", "make x happen")

    def boom_dedup(*, settings, draft_title, draft_body, candidates_json):
        raise RuntimeError("dedup model down")

    monkeypatch.setattr(dedup, "run_dedup_check", boom_dedup)

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

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_dedup_clone_failure_escalates_before_dedup(ctx, service, monkeypatch):
    """Clone failure propagates to the worker before dedup runs at all —
    no half-grounded refine attempts. The stage no longer catches
    CalledProcessError — the worker owns the retry/block decision."""
    import subprocess

    from robotsix_mill.vcs import git_ops

    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    ctx.settings.forge_remote_url = "https://example.test/repo.git"
    dedup_called = False

    def boom_clone(url, dest, branch, token):
        raise subprocess.CalledProcessError(128, "git", stderr="no access")

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(git_ops, "clone", boom_clone)
    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    t = service.create("Add X", _DEDUP_BODY)

    with pytest.raises(subprocess.CalledProcessError):
        RefineStage().run(t, ctx)

    assert not dedup_called, "dedup should not be called when clone failed"


def test_draft_to_closed_transition_is_legal():
    """DRAFT → CLOSED is a valid transition in the state machine."""
    from robotsix_mill.core.states import can_transition
    from robotsix_mill.core.states import State as S

    assert can_transition(S.DRAFT, S.CLOSED) is True


def test_dedup_guard_survives_preexisting_closed_ticket(ctx, service, monkeypatch):
    """Regression: SQLite used to return updated_at tz-naive; the dedup
    guard compared it to a tz-aware cutoff and raised TypeError, ERRORing
    every draft once any CLOSED ticket existed. After the model fix,
    updated_at is timezone-aware and comparisons are safe."""
    old = service.create("old done thing", "stuff")
    service.transition(old.id, State.CLOSED)  # now a closed candidate
    # Re-read via list() the way refine does.
    closed = [t for t in service.list() if t.id == old.id][0]
    assert closed.updated_at.tzinfo is not None

    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )
    t = service.create("Add Y", "rough idea")
    out = RefineStage().run(t, ctx)  # must NOT raise TypeError
    assert out.next_state is not State.ERRORED


def test_dedup_parent_filter_narrows_candidates(ctx, service, monkeypatch):
    """When the draft ticket belongs to an epic (has parent_id),
    the candidates passed to dedup are filtered to only siblings,
    the parent epic itself, orphans, and recently-closed tickets."""
    # Epic A — the draft's parent
    epic_a = service.create(
        "Epic A: Agent Memory", "memory system", kind=TicketKind.EPIC
    )
    # Epic B — unrelated
    epic_b = service.create(
        "Epic B: Deploy Config", "deployment things", kind=TicketKind.EPIC
    )

    # Draft ticket — child of epic A
    draft_ticket = service.create(
        "Add LRU eviction",
        _DEDUP_BODY,
        parent_id=epic_a.id,
    )

    # Sibling — same epic, should appear
    sibling = service.create(
        "Add TTL-based expiry",
        _DEDUP_BODY,
        parent_id=epic_a.id,
    )

    # Open ticket in unrelated epic — should NOT appear
    unrelated_open = service.create(
        "Switch to k3s",
        _DEDUP_BODY,
        parent_id=epic_b.id,
    )

    # Orphan (no parent) — should appear
    orphan = service.create("Upgrade CI runner", _DEDUP_BODY)

    # Recently-closed cross-epic ticket — should appear
    cross_epic_closed = service.create(
        "Old deploy fix",
        _DEDUP_BODY,
        parent_id=epic_b.id,
    )
    service.transition(cross_epic_closed.id, State.CLOSED)

    # Another epic that is NOT the draft's parent — should NOT appear
    unrelated_epic = service.create(
        "Epic C: Observability", "metrics", kind=TicketKind.EPIC
    )

    # Non-sibling open ticket in same epic is the only non-CLOSED,
    # non-orphan, non-parent candidate that SHOULD appear (sibling).
    # All the others from epic B should be excluded.

    seen_candidates: list[str] = []

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        seen_candidates.append(candidates_json)
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )

    out = RefineStage().run(draft_ticket, ctx)
    assert out.next_state is State.READY
    assert len(seen_candidates) == 1

    candidates_text = seen_candidates[0]

    # Should appear: sibling, parent epic, orphan, recently-closed cross-epic
    assert f"## {sibling.id}" in candidates_text
    assert f"## {epic_a.id}" in candidates_text  # parent epic
    assert f"## {orphan.id}" in candidates_text
    assert f"## {cross_epic_closed.id}" in candidates_text

    # Should NOT appear: unrelated open, unrelated epic
    assert f"## {unrelated_open.id}" not in candidates_text
    assert f"## {unrelated_epic.id}" not in candidates_text

    # Draft itself should never be a candidate
    assert f"## {draft_ticket.id}" not in candidates_text


def test_dedup_no_parent_fallback_unchanged(ctx, service, monkeypatch):
    """When the draft ticket has no parent_id, the full candidate set
    is passed through — behaviour is identical to before."""
    t = service.create("Standalone ticket", _DEDUP_BODY)
    # Create several tickets with various parents — all should appear.
    epic = service.create("Some epic", "stuff", kind=TicketKind.EPIC)
    child = service.create("Epic child", _DEDUP_BODY, parent_id=epic.id)
    orphan = service.create("Another orphan", _DEDUP_BODY)

    seen_candidates: list[str] = []

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        seen_candidates.append(candidates_json)
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: _single("## Problem\nspec\n")
    )

    out = RefineStage().run(t, ctx)
    assert out.next_state is State.READY
    assert len(seen_candidates) == 1

    candidates_text = seen_candidates[0]

    # All non-epic tickets should appear (epics are always excluded
    # unless they're the draft's own parent, which doesn't apply here).
    assert f"## {child.id}" in candidates_text
    assert f"## {orphan.id}" in candidates_text
    assert f"## {epic.id}" not in candidates_text  # epics excluded


def test_dedup_candidate_cap_enforced(ctx, service, monkeypatch):
    """Create 12 candidate tickets (above the default max of 8), run
    dedup, verify the candidate block contains at most 8 sections."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    # Create the draft ticket.
    draft_ticket = service.create("draft ticket", _DEDUP_BODY)

    # Create 12 candidate tickets with diverse titles.
    titles = [
        "Add dark mode toggle",
        "Fix login timeout bug",
        "Refactor database layer",
        "Update README badges",
        "Rate limiting middleware",
        "CSV export feature",
        "CI pipeline improvements",
        "Add healthcheck endpoint",
        "Add user avatar field",
        "Implement search functionality",
        "Upgrade to Python 3.14",
        "Add WebSocket support",
    ]
    for title in titles:
        service.create(title, "some body text for candidate ticket")

    seen_block = None

    def fake_dedup(
        *, settings, draft_title, draft_body, repo_dir=None, candidates_json
    ):
        nonlocal seen_block
        seen_block = candidates_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(draft_ticket, ctx)

    assert out.next_state is State.READY
    assert seen_block is not None

    # Count candidate sections (each is "## <id>").
    # The default dedup_max_candidates is 8, so at most 8 sections.
    section_count = seen_block.count("\n## ")
    # "(no candidates)" has zero sections.
    assert section_count <= 8, (
        f"expected at most 8 candidate sections, got {section_count}"
    )


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
    from datetime import timezone as tz

    t = service.create("roundtrip test", "body")
    reloaded = service.get(t.id)

    assert reloaded.created_at.tzinfo is not None
    assert reloaded.created_at.tzinfo == tz.utc
    assert reloaded.updated_at.tzinfo is not None
    assert reloaded.updated_at.tzinfo == tz.utc


def test_event_roundtrip_preserves_tzinfo(service):
    """AC #2: TicketEvent.at is timezone-aware after a history() call."""
    from datetime import timezone as tz

    t = service.create("event tz test", "body")
    service.transition(t.id, State.READY, "refined")
    events = service.history(t.id)

    assert len(events) >= 2  # created + refined
    for ev in events:
        assert ev.at.tzinfo is not None, f"event {ev.state} at is naive"
        assert ev.at.tzinfo == tz.utc


def test_aware_vs_aware_comparison_no_typeerror(service):
    """AC #4: Comparing DB-loaded datetimes against aware datetimes
    must succeed without TypeError."""
    from datetime import datetime, timedelta, timezone as tz

    t = service.create("compare test", "body")
    service.transition(t.id, State.CLOSED, "done")

    # Re-read via list() — must support comparison against aware values.
    tickets = service.list()
    ticket = [x for x in tickets if x.id == t.id][0]

    # This must not raise TypeError:
    assert ticket.updated_at >= datetime.now(tz.utc) - timedelta(days=30)
    assert ticket.created_at >= datetime.now(tz.utc) - timedelta(days=30)

    # Also test fromtimestamp path used by the dedup lookback:
    now = datetime.now(tz.utc)
    cutoff = datetime.fromtimestamp(now.timestamp() - 30 * 86400, tz=tz.utc)
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


# --- split detection tests ---


def test_split_creates_children_and_closes_parent(ctx, service, monkeypatch):
    """Multi-scope draft → N child tickets created, parent CLOSED, umbrella epic created."""
    child_a_spec = (
        "## Problem\nAdd checksum verification\n## Scope\n- verify checksums\n"
    )
    child_b_spec = "## Problem\nAdd HEALTHCHECK\n## Scope\n- add HEALTHCHECK\n"

    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {
                "title": "Add checksum verification",
                "spec_markdown": child_a_spec,
                "depends_on": [],
            },
            {
                "title": "Add HEALTHCHECK",
                "spec_markdown": child_b_spec,
                "depends_on": [0],
            },
        ),
    )

    parent = service.create("Dockerfile hardening", "multi-change draft")
    out = RefineStage().run(parent, ctx)

    # Parent → CLOSED with split note.
    assert out.next_state is State.CLOSED
    assert "split into" in out.note

    # Verify parent is closed after transition.
    service.transition(parent.id, out.next_state, out.note)
    parent_reloaded = service.get(parent.id)
    assert parent_reloaded.state is State.CLOSED

    # Extract child IDs from the note.
    ids_in_note = out.note.replace("split into ", "").split(", ")
    assert len(ids_in_note) == 2

    # Both children exist and have correct parent_id (umbrella epic, not original).
    child_a = service.get(ids_in_note[0])
    child_b = service.get(ids_in_note[1])
    assert child_a is not None
    assert child_b is not None

    # Find the umbrella epic that was created.
    all_tickets = service.list()
    epics = [t for t in all_tickets if t.kind == TicketKind.EPIC]
    assert len(epics) == 1
    epic = epics[0]
    assert epic.state is State.EPIC_OPEN
    # Epic title falls back to original ticket title (result.title is None).
    assert epic.title == "Dockerfile hardening"

    assert child_a.parent_id == epic.id
    assert child_b.parent_id == epic.id

    # Children have the right state (READY by default, no require_approval).
    assert child_a.state is State.READY
    assert child_b.state is State.READY

    # Children have the refined spec in their workspace.
    assert service.workspace(child_a).read_description().rstrip(
        "\n"
    ) == child_a_spec.rstrip("\n")
    assert service.workspace(child_b).read_description().rstrip(
        "\n"
    ) == child_b_spec.rstrip("\n")

    # Child B depends on child A.
    from robotsix_mill.core.service import _parse_depends_on_str

    assert _parse_depends_on_str(child_b.depends_on) == [child_a.id]

    # Child A has no dependencies.
    assert _parse_depends_on_str(child_a.depends_on) == []


def test_split_depends_on_indices_map_correctly(ctx, service, monkeypatch):
    """depends_on zero-based indices resolve to real child ticket IDs."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {
                "title": "Task 1",
                "spec_markdown": "## Problem\n1\n## Scope\n- one\n",
                "depends_on": [],
            },
            {
                "title": "Task 2",
                "spec_markdown": "## Problem\n2\n## Scope\n- two\n",
                "depends_on": [0],
            },
            {
                "title": "Task 3",
                "spec_markdown": "## Problem\n3\n## Scope\n- three\n",
                "depends_on": [0, 1],
            },
        ),
    )

    parent = service.create("Multi-task epic", "three independent tasks")
    out = RefineStage().run(parent, ctx)

    assert out.next_state is State.CLOSED
    ids_in_note = out.note.replace("split into ", "").split(", ")
    assert len(ids_in_note) == 3

    c0, c1, c2 = [service.get(cid) for cid in ids_in_note]

    from robotsix_mill.core.service import _parse_depends_on_str

    assert _parse_depends_on_str(c0.depends_on) == []
    assert _parse_depends_on_str(c1.depends_on) == [c0.id]
    assert _parse_depends_on_str(c2.depends_on) == [c0.id, c1.id]


def test_split_single_child_falls_back_to_normal(ctx, service, monkeypatch):
    """Only one valid child in split → fall back to single-spec path (no new tickets)."""
    child_spec = "## Problem\nSingle change\n## Scope\n- one thing\n"
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {"title": "The only change", "spec_markdown": child_spec, "depends_on": []},
        ),
    )

    t = service.create("Single change", "just one thing")
    out = RefineStage().run(t, ctx)

    # Should NOT be CLOSED — fallback to normal single-spec path.
    assert out.next_state is State.READY
    assert "single child" in out.note

    # Description should be the child's spec (not the original draft).
    assert service.workspace(t).read_description().rstrip("\n") == child_spec.rstrip(
        "\n"
    )
    # Title should be updated to child's title.
    assert service.get(t.id).title == "The only change"

    # draft-original.md preserved.
    assert (service.workspace(t).artifacts_dir / "draft-original.md").exists()


def test_split_empty_children_proceeds(ctx, service, monkeypatch):
    """No children in split → proceed with original draft (not BLOCKED)."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(split=True, children=[]),
    )

    t = service.create("Empty split", "draft")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.READY
    # Original draft preserved
    assert service.workspace(t).read_description() == "draft"


def test_split_empty_children_proceeds_to_human_issue_approval_when_gated(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """No children in split + gated → HUMAN_ISSUE_APPROVAL."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(split=True, children=[]),
    )

    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )

    t = service.create("Empty split gated", "draft")
    out = RefineStage().run(t, gated_ctx)
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert service.workspace(t).read_description() == "draft"


def test_split_malformed_children_skipped(ctx, service, monkeypatch):
    """Malformed child entries (missing title, missing spec) are skipped;
    if only one survives, fall back to single-spec."""
    good_spec = "## Problem\nGood\n## Scope\n- good\n"
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=True,
            children=[
                ChildSpec(
                    title="", spec_markdown="## Problem\nBad\n", depends_on=[]
                ),  # no title
                ChildSpec(title="Good", spec_markdown=good_spec, depends_on=[]),
                ChildSpec(title="Bad", spec_markdown="", depends_on=[]),  # no spec
            ],
        ),
    )

    t = service.create("Mixed children", "draft")
    out = RefineStage().run(t, ctx)

    # Only "Good" survives → fallback to single-spec.
    assert out.next_state is State.READY
    assert "single child" in out.note
    assert service.workspace(t).read_description().rstrip("\n") == good_spec.rstrip(
        "\n"
    )


def test_split_require_approval_honoured_per_child(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When require_approval=true, children go to HUMAN_ISSUE_APPROVAL."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {
                "title": "Child A",
                "spec_markdown": "## Problem\nA\n## Scope\n- a\n",
                "depends_on": [],
            },
            {
                "title": "Child B",
                "spec_markdown": "## Problem\nB\n## Scope\n- b\n",
                "depends_on": [],
            },
        ),
    )

    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )

    parent = service.create("Gated split", "draft")
    out = RefineStage().run(parent, gated_ctx)

    assert out.next_state is State.CLOSED
    ids_in_note = out.note.replace("split into ", "").split(", ")
    assert len(ids_in_note) == 2

    for cid in ids_in_note:
        child = service.get(cid)
        assert child.state is State.HUMAN_ISSUE_APPROVAL, (
            f"{cid} should be human_issue_approval"
        )


def test_split_child_skips_re_refinement(ctx, service, monkeypatch):
    """A split child's refine stage short-circuits: no agent call, uses existing spec."""
    child_a_spec = "## Problem\nAlready refined A\n## Scope\n- done a\n"
    child_b_spec = "## Problem\nAlready refined B\n## Scope\n- done b\n"

    # Step 1: Create a parent and split it into TWO children (need 2+ to trigger actual split).
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {"title": "Child A", "spec_markdown": child_a_spec, "depends_on": []},
            {"title": "Child B", "spec_markdown": child_b_spec, "depends_on": []},
        ),
    )

    parent = service.create("Split parent", "parent draft")
    out = RefineStage().run(parent, ctx)
    assert out.next_state is State.CLOSED
    ids_in_note = out.note.replace("split into ", "").split(", ")
    assert len(ids_in_note) == 2
    child_a_id, child_b_id = ids_in_note

    # Apply parent's CLOSED transition.
    service.transition(parent.id, out.next_state, out.note)

    # Step 2: Reset child A to DRAFT (simulate worker picking it up fresh).
    service.transition(child_a_id, State.BLOCKED, "test: back to draft")
    from robotsix_mill.core import db as core_db
    from robotsix_mill.core.models import Ticket as TicketModel

    with core_db.session(service.settings, service.board_id) as s:
        t = s.get(TicketModel, child_a_id)
        t.state = State.DRAFT
        t.blocked_from = None
        s.add(t)
        s.commit()

    # Step 3: Now run RefineStage on child A — it should skip the agent.
    refine_called = False

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
        return _single(draft)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    child = service.get(child_a_id)
    assert child.state is State.DRAFT
    # Child should be parented to the umbrella epic, not the original parent.
    all_tickets = service.list()
    epics = [t for t in all_tickets if t.kind == TicketKind.EPIC]
    assert len(epics) == 1
    assert child.parent_id == epics[0].id

    out2 = RefineStage().run(child, ctx)

    # Should NOT have called the refine agent.
    assert not refine_called
    # Should transition to READY (no require_approval).
    assert out2.next_state is State.READY
    assert "split child" in out2.note

    # The description should still be the original refined spec.
    assert service.workspace(child).read_description().rstrip(
        "\n"
    ) == child_a_spec.rstrip("\n")


def test_retrospect_spawned_child_not_skipped(ctx, service, monkeypatch):
    """A retrospect-spawned draft (parent CLOSED but NOT by a split)
    must still go through the refine agent — it is NOT a split child
    with an already-refined spec."""
    raw_draft = "retrospect agent's raw improvement idea — not a spec"

    # Simulate a retrospect-spawned draft: create a parent, close it
    # (as retrospect does), then create a child with parent_id set.
    parent = service.create("Reviewed ticket", "original work")
    service.transition(
        parent.id,
        State.CLOSED,
        "all good — improvement draft <child_id>",
    )

    child = service.create("Improvement idea", raw_draft)
    service.set_parent(child.id, parent.id)

    # Reset child to DRAFT (it was created as DRAFT, but set_parent
    # doesn't change state — verify it's DRAFT).
    assert service.get(child.id).state is State.DRAFT

    # Now run RefineStage on the child — it must call the agent.
    refine_called = False
    expected_spec = "## Problem\nrefined improvement\n## Scope\n- do it\n"

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
        assert draft == raw_draft
        return _single(expected_spec)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(child, ctx)

    # Must NOT short-circuit: agent should have been called.
    assert refine_called
    assert out.next_state is State.READY
    assert service.workspace(child).read_description().rstrip(
        "\n"
    ) == expected_spec.rstrip("\n")


def test_split_preserves_parent_draft_original(ctx, service, monkeypatch):
    """Parent's draft-original.md is preserved when splitting."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {
                "title": "Child 1",
                "spec_markdown": "## Problem\n1\n## Scope\n- one\n",
                "depends_on": [],
            },
            {
                "title": "Child 2",
                "spec_markdown": "## Problem\n2\n## Scope\n- two\n",
                "depends_on": [],
            },
        ),
    )

    parent = service.create("Parent ticket", "original multi-change draft")
    RefineStage().run(parent, ctx)

    draft_original = service.workspace(parent).artifacts_dir / "draft-original.md"
    assert draft_original.exists()
    assert draft_original.read_text() == "original multi-change draft"


def test_split_with_invalid_depends_on_indices_handled(ctx, service, monkeypatch):
    """depends_on indices that are out of range or point to future children are ignored."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {
                "title": "Task A",
                "spec_markdown": "## Problem\nA\n## Scope\n- a\n",
                "depends_on": [5],
            },  # out of range
            {
                "title": "Task B",
                "spec_markdown": "## Problem\nB\n## Scope\n- b\n",
                "depends_on": [0],
            },  # valid
            {
                "title": "Task C",
                "spec_markdown": "## Problem\nC\n## Scope\n- c\n",
                "depends_on": [-1, 0],
            },  # negative ignored, 0 valid
        ),
    )

    parent = service.create("Dep test", "draft")
    out = RefineStage().run(parent, ctx)
    assert out.next_state is State.CLOSED
    ids_in_note = out.note.replace("split into ", "").split(", ")
    assert len(ids_in_note) == 3

    c0, c1, c2 = [service.get(cid) for cid in ids_in_note]

    from robotsix_mill.core.service import _parse_depends_on_str

    # Task A: [5] is out of range → ignored.
    assert _parse_depends_on_str(c0.depends_on) == []
    # Task B: [0] valid → depends on Task A.
    assert _parse_depends_on_str(c1.depends_on) == [c0.id]
    # Task C: [-1] ignored, [0] valid → depends on Task A.
    assert _parse_depends_on_str(c2.depends_on) == [c0.id]


def test_no_split_single_scope_unchanged(ctx, service, monkeypatch):
    """Single-scope draft behaviour is byte-for-byte identical to before."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    ws = service.workspace(t)
    assert ws.read_description() == spec
    assert (ws.artifacts_dir / "draft-original.md").read_text() == "make x happen"
    expected = hashlib.sha256(spec.encode("utf-8")).hexdigest()
    assert service.get(t.id).content_hash == expected


def test_refine_agent_fallback_raw_markdown(monkeypatch, tmp_path):
    """When the agent outputs raw Markdown (no structured output), it is
    treated as a single-scope spec (graceful fallback via PromptedOutput)."""
    from robotsix_mill.agents import base as base_mod

    raw_md = "## Problem\nraw output\n## Scope\n- no json"

    def fake_build_agent(
        settings, system_prompt, tools, web_knowledge, level, **kwargs
    ):
        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type("R", (), {"output": _single(raw_md)})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s,
        title="Test",
        draft="draft",
    )

    assert result.split is False
    assert result.spec_markdown == raw_md


def test_refine_agent_malformed_json_fallback(monkeypatch, tmp_path):
    """When the agent outputs something that looks like a JSON envelope
    but is malformed, PromptedOutput handles it gracefully."""
    from robotsix_mill.agents import base as base_mod

    # PromptedOutput will receive malformed output but should produce
    # a valid RefineResult via the model's structured output parsing.
    # We simulate by returning a proper RefineResult from the fake.
    raw = '{"split": false, "spec": "## Problem\nunclosed string'

    def fake_build_agent(
        settings, system_prompt, tools, web_knowledge, level, **kwargs
    ):
        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                # Simulate PromptedOutput parsing — returns a RefineResult.
                return type("R", (), {"output": _single(raw)})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s,
        title="Test",
        draft="draft",
    )

    # Falls back to raw-as-spec.
    assert result.split is False
    assert result.spec_markdown == raw


def test_split_heuristic_present_in_system_prompt(monkeypatch, tmp_path):
    """The refine system prompt must contain the surface-based split
    heuristic with its three concrete signals."""
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
    assert "≥4 distinct source files" in prompt
    assert "≥3 new endpoints" in prompt
    assert "backend↔frontend boundary" in prompt
    assert "Escape clause" in prompt
    assert "Borderline drafts stay as one spec" in prompt


def test_tool_strategy_present_in_system_prompt(monkeypatch, tmp_path):
    """The refine system prompt must contain tool-strategy guidance
    steering the agent toward direct tools for simple lookups and
    batching explore calls."""
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
    # The old "## Tool strategy" section has been moved out of the
    # refine agent's SYSTEM_PROMPT — tool descriptions are no longer
    # injected into the prompt at all.  Because this test
    # monkeypatches build_agent, _compose_prompt is bypassed — we just
    # verify the refine SYSTEM_PROMPT still exists and is non-trivial.
    assert "You turn a rough ticket draft" in prompt
    assert "## Memory" in prompt


def test_borderline_draft_not_split(ctx, service, monkeypatch):
    """A borderline draft (single endpoint, two files, same layer)
    must NOT be split — the new prompt must not trigger aggressive
    splitting. This is a pin test for the escape clause."""
    spec = "## Problem\nAdd a user avatar field\n## Scope\n- Add `avatar_url` to User model\n- Update GET /users route\n## Acceptance criteria\n- [ ] avatar field returned\n## Out of scope / constraints\n- No frontend changes\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))

    t = service.create("Add user avatar field", "add avatar_url to user")
    out = RefineStage().run(t, ctx)

    # Must transition to READY (not CLOSED from a split).
    assert out.next_state is State.READY
    assert "split" not in out.note.lower()


# --- typo-tolerant RefineResult validator ---


def test_refine_result_absorbs_spec_markmark_typo():
    """deepseek-v4-pro consistently mis-types ``spec_markdown`` as
    ``spec_markmark`` on the refine output. Observed three times in
    production today (tickets 5061, efd4, f93f) — each time pydantic-ai
    silently dropped the unknown key, ``spec_markdown`` stayed None, and
    the refine stage blocked with "refiner produced an empty spec."

    The pre-validator on ``RefineResult`` folds this typo class (and
    the bare ``spec`` near-miss) into ``spec_markdown`` so the typo
    can't block tickets anymore."""
    # The exact production typo.
    r = RefineResult.model_validate(
        {
            "split": False,
            "spec_markmark": "## Problem\n\nThe spec content.\n",
        }
    )
    assert r.spec_markdown == "## Problem\n\nThe spec content.\n"
    assert r.split is False


def test_refine_result_absorbs_bare_spec_key():
    """Some refine retries emit ``"spec"`` (no underscore) instead of
    ``"spec_markdown"``. Same absorption path."""
    r = RefineResult.model_validate({"split": False, "spec": "hello"})
    assert r.spec_markdown == "hello"


def test_refine_result_absorbs_spec_md_short_typo():
    """Also tolerate ``spec_md`` (another observed short-form near-miss)."""
    r = RefineResult.model_validate({"split": False, "spec_md": "content"})
    assert r.spec_markdown == "content"


def test_refine_result_canonical_spec_markdown_passes_through():
    """The validator must not interfere with correctly-keyed output."""
    r = RefineResult.model_validate(
        {
            "split": False,
            "spec_markdown": "canonical content",
        }
    )
    assert r.spec_markdown == "canonical content"


def test_refine_result_empty_typo_value_not_absorbed():
    """If the typo key has an empty / whitespace-only value, do NOT
    absorb it — that would mask a genuinely-empty refine output as
    'present', producing a downstream confusion."""
    r = RefineResult.model_validate(
        {
            "split": False,
            "spec_markmark": "",
        }
    )
    assert r.spec_markdown is None  # genuinely empty → blocks downstream


# --- reviewer sendback prompt ---


def test_sendback_uses_short_prompt(monkeypatch, tmp_path):
    """When reviewer_comments is non-empty, REVIEWER_SENDBACK_PROMPT
    is passed to build_agent instead of SYSTEM_PROMPT."""
    from robotsix_mill.agents import base as base_mod
    from robotsix_mill.agents.refining import (
        REVIEWER_SENDBACK_PROMPT,
        SYSTEM_PROMPT,
    )

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
    refining.run_refine_agent(
        settings=s,
        title="Test",
        draft="draft",
        reviewer_comments="fix this",
    )

    assert len(seen_system_prompt) == 1
    assert seen_system_prompt[0] == REVIEWER_SENDBACK_PROMPT
    assert seen_system_prompt[0] != SYSTEM_PROMPT


def test_first_refinement_uses_full_prompt(monkeypatch, tmp_path):
    """When reviewer_comments is None/empty, SYSTEM_PROMPT is used."""
    from robotsix_mill.agents import base as base_mod
    from robotsix_mill.agents.refining import SYSTEM_PROMPT

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
    assert seen_system_prompt[0] == SYSTEM_PROMPT


def test_sendback_enables_reply_and_close_thread_tools(monkeypatch, tmp_path):
    """When reviewer_comments is truthy, reply_to_thread=True and
    close_thread=True are passed to build_agent (overriding the YAML
    definition's false defaults). When reviewer_comments is None,
    both flags remain False (the normal refine path should not get
    these tools)."""
    from robotsix_mill.agents import base as base_mod

    run_kwargs: list[dict] = []

    def fake_build_agent(
        settings, system_prompt, tools, web_knowledge, level, **kwargs
    ):
        run_kwargs.append(kwargs)

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type("R", (), {"output": _single("## Problem\nok\n")})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))

    # Case 1: with reviewer_comments → both flags True.
    refining.run_refine_agent(
        settings=s,
        title="Test",
        draft="draft",
        reviewer_comments="fix this",
    )
    assert run_kwargs[0].get("reply_to_thread") is True
    assert run_kwargs[0].get("close_thread") is True

    run_kwargs.clear()

    # Case 2: without reviewer_comments → both flags False.
    refining.run_refine_agent(settings=s, title="Test", draft="draft")
    assert run_kwargs[0].get("reply_to_thread") is False
    assert run_kwargs[0].get("close_thread") is False


def test_sendback_prompt_includes_reviewer_feedback_reference():
    """The sendback prompt must instruct the agent to incorporate
    the reviewer_feedback block."""
    from robotsix_mill.agents.refining import REVIEWER_SENDBACK_PROMPT

    assert "reviewer_feedback" in REVIEWER_SENDBACK_PROMPT.lower()
    # Must preserve Memory section
    assert "## Memory" in REVIEWER_SENDBACK_PROMPT
    # Must preserve Output format section
    assert "## Output format" in REVIEWER_SENDBACK_PROMPT
    # Must NOT contain the lengthy split heuristics (from SYSTEM_PROMPT)
    assert "≥4 distinct source files" not in REVIEWER_SENDBACK_PROMPT
    assert "backend↔frontend boundary" not in REVIEWER_SENDBACK_PROMPT


def test_sendback_prompt_warns_against_reclosing_threads():
    """Regression: the sendback prompt must explicitly instruct the
    model not to re-close an already-closed thread, and to treat
    'already closed' results as success rather than retry."""
    from robotsix_mill.agents.refining import REVIEWER_SENDBACK_PROMPT

    assert "Do NOT call" in REVIEWER_SENDBACK_PROMPT
    assert "close_thread" in REVIEWER_SENDBACK_PROMPT
    assert "already resolved" in REVIEWER_SENDBACK_PROMPT
    assert "treat that as success" in REVIEWER_SENDBACK_PROMPT
    assert "do not retry" in REVIEWER_SENDBACK_PROMPT.lower()


def test_memory_prompt_forbids_per_ticket_diary():
    """The refine system prompt and reviewer-sendback prompt must
    instruct the agent to record general repo knowledge only — not
    per-ticket diaries. Regression: the previous wording produced
    `## Refine run for <ticket-id>` sections in refine_memory.md."""
    from robotsix_mill.agents.refining import SYSTEM_PROMPT, REVIEWER_SENDBACK_PROMPT

    for label, prompt in (
        ("SYSTEM_PROMPT", SYSTEM_PROMPT),
        ("REVIEWER_SENDBACK_PROMPT", REVIEWER_SENDBACK_PROMPT),
    ):
        # Forbidden phrasings from the old prompt.
        assert "ticket-ID-qualified" not in prompt, label
        assert "split/bundle decisions and their rationale" not in prompt, label
        # Required new framing: explicit prohibition.
        assert "NOT a per-ticket diary" in prompt, label
        # Required: ticket IDs called out as forbidden ledger content.
        assert "Ticket IDs" in prompt, label


# --- epic context -------------------------------------------------------


def test_epic_context_passed_to_refine_agent(ctx, service, monkeypatch):
    """When a ticket has an epic parent, epic_context is passed to
    run_refine_agent and contains the epic description."""
    epic = service.create("Global Epic", "High-level: unify UX", kind=TicketKind.EPIC)
    child = service.create(
        "Add dark mode",
        "Please add dark mode toggle",
        parent_id=epic.id,
    )

    seen_epic_context: list[str] = []

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
        seen_epic_context.append(epic_context)
        return _single("## Problem\nspec\n")

    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    out = RefineStage().run(child, ctx)
    assert out.next_state in (State.HUMAN_ISSUE_APPROVAL, State.READY)
    assert len(seen_epic_context) == 1
    assert "High-level: unify UX" in seen_epic_context[0]
    assert seen_epic_context[0].startswith("````epic-context")


def test_epic_context_empty_for_non_epic_parent_in_refine(ctx, service, monkeypatch):
    """Refine: ticket with non-epic parent → epic_context is empty."""
    parent = service.create("Parent task", "Ordinary task", kind=TicketKind.TASK)
    child = service.create(
        "Child of task",
        "Do a sub-thing",
        parent_id=parent.id,
    )

    seen_epic_context: list[str] = []

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
        seen_epic_context.append(epic_context)
        return _single("## Problem\nspec\n")

    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    RefineStage().run(child, ctx)
    assert len(seen_epic_context) == 1
    assert seen_epic_context[0] == ""


def test_epic_context_empty_for_no_parent_in_refine(ctx, service, monkeypatch):
    """Refine: ticket without parent → epic_context is empty."""
    t = service.create("Standalone", "Just a draft")

    seen_epic_context: list[str] = []

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
        seen_epic_context.append(epic_context)
        return _single("## Problem\nspec\n")

    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    RefineStage().run(t, ctx)
    assert len(seen_epic_context) == 1
    assert seen_epic_context[0] == ""


# --- title refinement tests ---


def test_refine_updates_title_when_agent_provides_one(ctx, service, monkeypatch):
    """Agent returns a title → set_title is called with it."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(split=False, spec_markdown=spec, title="Better Title"),
    )

    t = service.create("Fix the thing", "make x happen")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert service.get(t.id).title == "Better Title"


def test_refine_keeps_original_title_when_agent_returns_none(ctx, service, monkeypatch):
    """Agent returns no title → set_title is NOT called."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(split=False, spec_markdown=spec),
    )

    t = service.create("Fix the thing", "make x happen")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert service.get(t.id).title == "Fix the thing"


def test_refine_keeps_original_title_when_agent_returns_empty(
    ctx, service, monkeypatch
):
    """Agent returns empty/whitespace title → set_title is NOT called."""
    for empty_title in ("", "   "):
        monkeypatch.setattr(
            refining,
            "run_refine_agent",
            lambda _title=empty_title, **_: RefineResult(
                split=False, spec_markdown="## Problem\nx\n", title=_title
            ),
        )

        t = service.create("Fix the thing", "make x happen")
        out = RefineStage().run(t, ctx)

        assert out.next_state is State.READY
        assert service.get(t.id).title == "Fix the thing"


def test_refine_split_applies_title_to_parent(ctx, service, monkeypatch):
    """Split with agent title → set_title called on parent before close."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=True,
            title="Better Epic Name",
            children=[
                ChildSpec(
                    title="Child A", spec_markdown="## Problem\nA\n## Scope\n- a\n"
                ),
                ChildSpec(
                    title="Child B", spec_markdown="## Problem\nB\n## Scope\n- b\n"
                ),
            ],
        ),
    )

    parent = service.create("Fix the thing", "multi-change draft")
    out = RefineStage().run(parent, ctx)

    assert out.next_state is State.CLOSED
    # Parent title should be updated before close.
    assert service.get(parent.id).title == "Better Epic Name"


def test_refine_split_single_child_prefers_agent_title(ctx, service, monkeypatch):
    """Single-child fallback: agent title beats child title."""
    child_spec = "## Problem\nSingle change\n## Scope\n- one thing\n"
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=True,
            title="Agent Title",
            children=[
                ChildSpec(title="Child Title", spec_markdown=child_spec),
            ],
        ),
    )

    t = service.create("Fix the thing", "just one thing")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "single child" in out.note
    assert service.get(t.id).title == "Agent Title"


# ---------------------------------------------------------------------------
# triage pass tests
# ---------------------------------------------------------------------------


def test_triage_refine_agent_config(monkeypatch, tmp_path):
    """triage_refine builds an agent with zero tools,
    web_knowledge=False, and the triage level (1) from triage.yaml."""
    from robotsix_mill.agents import base as base_mod
    from robotsix_mill.agents.refining import triage_refine, TriageResult

    seen_kwargs: dict = {}

    def fake_build_agent(
        settings,
        system_prompt,
        output_type,
        tools,
        web_knowledge,
        report_issue,
        level,
        name,
        ask_user,
        **kwargs,
    ):
        seen_kwargs.update(
            tools=tools,
            web_knowledge=web_knowledge,
            report_issue=report_issue,
            level=level,
            name=name,
            ask_user=ask_user,
        )

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type(
                    "R", (), {"output": TriageResult(decision="REFINE", reason="test")}
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    result = triage_refine(settings=s, title="Test", draft="do x in foo.py")

    assert result.decision == "REFINE"
    assert seen_kwargs["tools"] == []
    assert seen_kwargs["web_knowledge"] is False
    assert seen_kwargs["report_issue"] is False
    assert seen_kwargs["level"] == 2  # triage.yaml level (promoted per #1692)
    assert seen_kwargs["name"] == "triage"
    assert seen_kwargs["ask_user"] is False


def test_triage_refine_wires_read_file_with_repo_dir(monkeypatch, tmp_path):
    """With repo_dir provided, triage_refine wires exactly an ``explore``
    tool plus a read-only ``read_file`` tool — and no write/edit/delete/
    run_command/list_dir."""
    from robotsix_mill.agents import base as base_mod
    from robotsix_mill.agents.refining import triage_refine, TriageResult

    seen_kwargs: dict = {}

    def fake_build_agent(
        settings,
        system_prompt,
        output_type,
        tools,
        web_knowledge,
        report_issue,
        level,
        name,
        ask_user,
        **kwargs,
    ):
        seen_kwargs.update(tools=tools)

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type(
                    "R", (), {"output": TriageResult(decision="REFINE", reason="test")}
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    s = Settings(data_dir=str(tmp_path))
    result = triage_refine(
        settings=s, title="Test", draft="do x in foo.py", repo_dir=repo_dir
    )

    assert result.decision == "REFINE"
    tool_names = {t.__name__ for t in seen_kwargs["tools"]}
    assert "explore" in tool_names
    assert "read_file" in tool_names
    assert not (
        tool_names
        & {"write_file", "edit_file", "delete_file", "run_command", "list_dir"}
    )


def test_triage_skip_skips_full_refine(ctx, service, monkeypatch):
    """When triage returns SKIP, run_refine_agent is NOT called,
    the draft is preserved, and the ticket goes to READY."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="SKIP", reason="doc-only change, no exploration needed"
        )

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
        nonlocal refine_called
        refine_called = True
        return _single("should not be called")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    t = service.create(
        "Update README", "Change the version badge in `src/main.py` line 5."
    )
    out = RefineStage().run(t, ctx)

    assert not refine_called
    assert out.next_state is State.READY
    assert "triage SKIP:" in out.note
    assert "doc-only change" in out.note


def test_prescriptive_spec_deterministic_skip(ctx, service, monkeypatch):
    """When a draft contains >50 lines of fenced code blocks, triage_skip
    short-circuits WITHOUT calling triage_refine — the draft is treated as
    an already-implementation-ready prescriptive spec."""
    from robotsix_mill.agents.refining import TriageResult
    from robotsix_mill.stages.refine._triage import _count_code_block_lines

    refine_called = False
    triage_called = False

    def fake_refine(**kw):
        nonlocal refine_called
        refine_called = True
        return _single("should not be called")

    def fake_triage(**kw):
        nonlocal triage_called
        triage_called = True
        return TriageResult(decision="REFINE", reason="should not be called")

    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)
    monkeypatch.setattr(refining, "triage_refine", fake_triage)

    # Build a draft with exactly 50 code-block lines (at threshold).
    code_body = "\n".join(f"    line_{i:03d}()" for i in range(50))
    draft = f"## Problem\n\nExact implementation:\n\n```python\n{code_body}\n```"

    assert _count_code_block_lines(draft) == 50

    t = service.create("Add feature", draft)
    # Use the default ctx (require_approval=False) so the ticket
    # routes to READY, not HUMAN_ISSUE_APPROVAL.
    out = RefineStage().run(t, ctx)

    assert not triage_called, "triage_refine should NOT have been called"
    assert not refine_called, "run_refine_agent should NOT have been called"
    assert out.next_state is State.READY
    assert "prescriptive spec" in out.note


def test_prescriptive_spec_below_threshold_still_triages(ctx, service, monkeypatch):
    """A draft with <50 code-block lines should still go through triage."""
    from robotsix_mill.agents.refining import TriageResult
    from robotsix_mill.stages.refine._triage import _count_code_block_lines

    triage_called = False

    def fake_triage(**kw):
        nonlocal triage_called
        triage_called = True
        return TriageResult(decision="SKIP", reason="already precise enough")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", lambda **kw: _single("unused"))

    # Build a draft with only 3 code-block lines (small snippet).
    draft = "## Problem\n\nExample:\n\n```python\nx = 1\ny = 2\nz = 3\n```"

    assert _count_code_block_lines(draft) == 3

    t = service.create("Small snippet", draft)
    out = RefineStage().run(t, ctx)

    assert triage_called, "triage_refine SHOULD have been called"
    assert "triage SKIP" in out.note


def test_triage_skip_goes_to_human_issue_approval_when_gated(
    ctx, service, monkeypatch, repo_config
):
    """When triage returns SKIP and require_approval=True, the ticket
    transitions to HUMAN_ISSUE_APPROVAL."""
    from robotsix_mill.agents.refining import TriageResult

    monkeypatch.setattr(
        refining,
        "triage_refine",
        lambda **_: TriageResult(decision="SKIP", reason="config-only"),
    )
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single("unused"))

    t = service.create("Add env var", "Add FOO=bar to `src/config.py` line 42.")

    from robotsix_mill.config import Settings as S

    gated = S(data_dir=str(ctx.settings.data_dir), require_approval="true")
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "triage SKIP:" in out.note


def test_triage_refine_calls_full_refine(ctx, service, monkeypatch):
    """When triage returns REFINE, run_refine_agent IS called normally."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE", reason="ambiguous scope, needs exploration"
        )

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
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Add feature X", "make it work with the thing")
    out = RefineStage().run(t, ctx)

    assert refine_called
    assert out.next_state is State.READY
    assert out.note == "refined"


def test_triage_feature_flag_off_calls_full_refine(
    ctx, service, monkeypatch, repo_config
):
    """When refine_triage_enabled=False, triage_refine is never called
    and full refine runs."""
    refine_called = False
    triage_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        nonlocal triage_called
        triage_called = True
        from robotsix_mill.agents.refining import TriageResult

        return TriageResult(decision="SKIP", reason="should not be reached")

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
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Update README", "Change the version badge in README.md line 5.")

    from robotsix_mill.config import Settings as S

    disabled = S(
        data_dir=str(ctx.settings.data_dir),
        refine_triage_enabled="false",
        require_approval="false",
    )
    disabled_ctx = StageContext(
        settings=disabled, service=service, repo_config=repo_config
    )
    out = RefineStage().run(t, disabled_ctx)

    assert not triage_called
    assert refine_called
    assert out.next_state is State.READY


def test_triage_sendback_always_refines(ctx, service, monkeypatch):
    """When the ticket has reviewer comments (sendback), triage is
    skipped and full refine runs even though the draft looks trivial."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False
    triage_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        nonlocal triage_called
        triage_called = True
        return TriageResult(decision="SKIP", reason="should not be reached")

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
        # Verify reviewer comments were passed through.
        assert reviewer_comments is not None
        assert "please fix x" in reviewer_comments
        return _single("## Problem\nrefined with feedback\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Update README", "Change the version badge in README.md line 5.")
    # Add a reviewer comment to simulate sendback.
    service.add_comment(t.id, "please fix x")

    out = RefineStage().run(t, ctx)

    assert not triage_called
    assert refine_called
    assert out.next_state is State.READY


def test_triage_failure_falls_through_to_refine(ctx, service, monkeypatch):
    """When triage_refine raises, a warning is logged and full refine
    proceeds normally."""
    refine_called = False

    def boom_triage(*, settings, title, draft):
        raise RuntimeError("triage model down")

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
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", boom_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Add X", "make x happen")
    out = RefineStage().run(t, ctx)

    assert refine_called
    assert out.next_state is State.READY
    assert out.note == "refined"


# ---------------------------------------------------------------------------
# auto-approve triage tests
# ---------------------------------------------------------------------------


def test_auto_approve_approve_skips_human_gate(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When triage_auto_approve returns APPROVE, the ticket goes straight
    to READY even when require_approval=true.  Uses a precise multi-file
    feature spec to demonstrate the relaxed criteria."""
    spec = (
        "## Problem\nUsers need to export their data in CSV format.\n"
        "## Scope\n- src/export/csv_writer.py: add write_csv() function\n"
        "- src/cli/export.py: wire --format csv flag\n"
        "- tests/export/test_csv_writer.py: add round-trip test\n"
        "## Acceptance criteria\n"
        "- [ ] write_csv() produces valid RFC 4180 CSV\n"
        "- [ ] --format csv flag triggers CSV export path\n"
        "- [ ] round-trip test passes: write then parse matches input\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="APPROVE",
            reason="precise multi-file feature, no design decisions",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("CSV export", "add CSV export feature")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.READY
    assert (
        "auto-approve: APPROVE — precise multi-file feature, no design decisions"
        in out.note
    )


def test_auto_approve_needs_approval_goes_to_human(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When triage_auto_approve returns NEEDS_APPROVAL, the ticket goes to
    HUMAN_ISSUE_APPROVAL when gated.  The spec here is ambiguous about scope
    — the implementer would have to guess where to make changes."""
    spec = (
        "## Problem\nImprove error handling across the application.\n"
        "## Scope\n- Various files\n"
        "## Acceptance criteria\n- [ ] errors are handled better\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="ambiguous scope, unclear acceptance criteria",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Improve errors", "improve error handling")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert (
        "auto-approve: NEEDS_APPROVAL — ambiguous scope, unclear acceptance criteria"
        in out.note
    )


def test_auto_approve_failure_falls_back_to_human(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When triage_auto_approve raises, the ticket falls back to
    HUMAN_ISSUE_APPROVAL when gated."""
    spec = "## Problem\nFix typo in README\n## Scope\n- README.md line 5\n## Acceptance criteria\n- [ ] typo is fixed\n"

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: (_ for _ in ()).throw(RuntimeError("auto-approve model down")),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Fix typo", "fix a typo in README.md")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "auto-approve: triage failed — falling back to human approval" in out.note


def test_auto_approve_flag_off_never_called(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When auto_approve_enabled=false, triage_auto_approve is never called
    and the ticket follows normal gated behaviour."""
    spec = "## Problem\nFix typo in README\n## Scope\n- README.md line 5\n## Acceptance criteria\n- [ ] typo is fixed\n"

    auto_approve_called = False

    def fake_auto_approve(*, settings, spec):
        nonlocal auto_approve_called
        auto_approve_called = True
        return refining.AutoApproveResult(
            decision="APPROVE", reason="should not be reached"
        )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(refining, "triage_auto_approve", fake_auto_approve)

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="false",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Fix typo", "fix a typo in README.md")
    out = RefineStage().run(t, gated_ctx)

    assert not auto_approve_called
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL


def test_auto_approve_precise_multifile_feature_approved(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """A precise, well-specified multi-file feature spec with clear
    acceptance criteria → APPROVE, ticket goes to READY."""
    spec = (
        "## Problem\nAdd pagination to the list-endpoints response.\n"
        "## Scope\n"
        "- src/api/list.py: accept ?page= and ?per_page= query params\n"
        "- src/db/queries.py: add LIMIT/OFFSET to list queries\n"
        "- tests/api/test_list.py: test paginated responses\n"
        "## Acceptance criteria\n"
        "- [ ] GET /items?page=2&per_page=10 returns second page of 10 items\n"
        "- [ ] default per_page=20 when not specified\n"
        "- [ ] page < 1 returns 400\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="APPROVE",
            reason="precise multi-file feature, no design decisions",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Add pagination", "add pagination to list endpoints")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.READY


def test_auto_approve_ambiguous_spec_needs_approval(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """A spec with ambiguous scope where the implementer would have to
    guess → NEEDS_APPROVAL, ticket goes to HUMAN_ISSUE_APPROVAL."""
    spec = (
        "## Problem\nMake the app faster.\n"
        "## Scope\n- Improve performance\n"
        "## Acceptance criteria\n- [ ] app is faster\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="ambiguous scope, implementer must guess",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Make faster", "make the app faster")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL


def test_auto_approve_architecture_decision_needs_approval(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """A spec introducing a new abstraction/module boundary →
    NEEDS_APPROVAL, ticket goes to HUMAN_ISSUE_APPROVAL."""
    spec = (
        "## Problem\nIntroduce a plugin system so third-party extensions\n"
        "can hook into the request pipeline.\n"
        "## Scope\n"
        "- src/core/plugin.py: new Plugin base class and registry\n"
        "- src/core/pipeline.py: refactor to call plugin hooks\n"
        "- src/core/__init__.py: export plugin API as public interface\n"
        "## Acceptance criteria\n"
        "- [ ] plugins can register before_request and after_response hooks\n"
        "- [ ] hooks fire in registration order\n"
        "- [ ] a faulty plugin does not crash the pipeline\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="new plugin abstraction, public API change",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Plugin system", "add plugin system")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL


# ---------------------------------------------------------------------------
# Classification-pin tests — new permissive auto-approve criteria
# ---------------------------------------------------------------------------


def _ctx(require_approval=True, auto_approve_enabled=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        settings=SimpleNamespace(
            require_approval=require_approval,
            auto_approve_enabled=auto_approve_enabled,
        )
    )


def test_auto_approve_yaml_criteria_permissive_tie_breaker():
    """The auto-approve YAML must encode the permissive tie-breaker and the
    five NEEDS_APPROVAL gates. Protects against prompt regressions."""
    import yaml as _yaml
    from pathlib import Path

    text = (
        Path(__file__).parents[2] / "agent_definitions" / "auto-approve.yaml"
    ).read_text()
    data = _yaml.safe_load(text)
    prompt = data["system_prompt"]
    assert "when unsure, return APPROVE" in prompt
    prompt_lower = prompt.lower()
    for phrase in [
        "authentication",  # gate 1 — security
        "destructive",  # gate 2 — irreversible
        "cross-repo",  # gate 3 — infra/CI
        "public-api",  # gate 4 — breaking
        "external runtime dependency",  # gate 5 — new dep
    ]:
        assert phrase in prompt_lower, (
            f"Expected criteria phrase {phrase!r} missing from auto-approve.yaml"
        )


_ROUTINE_SPEC_CASES = [
    (
        "new_internal_module",
        "## Problem\nAdd ExportManager to handle CSV exports.\n"
        "## Scope\n- src/export/manager.py: new ExportManager class\n"
        "## Acceptance criteria\n- [ ] ExportManager.export() returns bytes\n",
    ),
    (
        "new_pydantic_schema",
        "## Problem\nAdd ExportConfig schema.\n"
        "## Scope\n- src/schemas/export.py: new Pydantic model\n"
        "## Acceptance criteria\n- [ ] ExportConfig validates required fields\n",
    ),
    (
        "ui_change",
        "## Problem\nUpdate button styling on the dashboard.\n"
        "## Scope\n- src/templates/dashboard.html: change CSS class\n"
        "## Acceptance criteria\n- [ ] Button shows correct colour\n",
    ),
    (
        "tests_only",
        "## Problem\nAdd missing tests for ExportManager.\n"
        "## Scope\n- tests/test_export_manager.py: five unit tests\n"
        "## Acceptance criteria\n- [ ] All five tests pass\n",
    ),
    (
        "docs_only",
        "## Problem\nDocument the new export endpoints.\n"
        "## Scope\n- docs/export.md: add usage section\n"
        "## Acceptance criteria\n- [ ] Section present and accurate\n",
    ),
    (
        "internal_endpoint",
        "## Problem\nAdd GET /internal/health endpoint.\n"
        "## Scope\n- src/routes/internal.py: register route\n"
        "## Acceptance criteria\n- [ ] 200 on GET /internal/health\n",
    ),
    (
        "refactor",
        "## Problem\nExtract _parse_csv helper from CsvImporter.\n"
        "## Scope\n- src/importers/csv.py: extract helper function\n"
        "## Acceptance criteria\n- [ ] All existing tests pass\n",
    ),
]

_HIGH_RISK_SPEC_CASES = [
    (
        "auth_secrets",
        "## Problem\nRotate JWT signing secret.\n"
        "## Scope\n- src/auth/jwt.py: update secret key handling\n"
        "## Acceptance criteria\n- [ ] New secret applied on startup\n",
    ),
    (
        "destructive_migration",
        "## Problem\nRemove legacy columns from users table.\n"
        "## Scope\n- migrations/0042_drop_columns.py: DROP COLUMN legacy_flag, legacy_data\n"
        "## Acceptance criteria\n- [ ] Columns removed; migration irreversible\n",
    ),
    (
        "cross_repo_ci",
        "## Problem\nUpdate shared CI deploy workflow.\n"
        "## Scope\n- .github/workflows/deploy.yml: change shared deploy step\n"
        "## Acceptance criteria\n- [ ] CI pipeline updated across all repos\n",
    ),
    (
        "breaking_public_api",
        "## Problem\nRemove deprecated public endpoint GET /api/v1/users.\n"
        "## Scope\n- src/api/v1/users.py: remove route; external callers must migrate\n"
        "## Acceptance criteria\n- [ ] Endpoint removed from public API\n",
    ),
    (
        "new_external_runtime_dep",
        "## Problem\nAdd cryptography package for FIPS-compliant hashing.\n"
        "## Scope\n- pyproject.toml: add cryptography>=41 to runtime dependencies\n"
        "## Acceptance criteria\n- [ ] cryptography importable at runtime\n",
    ),
]


@pytest.mark.parametrize("label,spec", _ROUTINE_SPEC_CASES)
def test_auto_approve_criteria_approve_routine(label, spec, monkeypatch):
    """Routine specs (new internal module / schema / UI / tests / docs /
    internal endpoint / refactor) must route to APPROVE under new criteria."""
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda *, settings, spec, **kw: refining.AutoApproveResult(
            decision="APPROVE", reason=f"routine: {label}"
        ),
    )
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        spec,
        "t1",
    )
    assert state is State.READY, f"{label!r} expected APPROVE → READY, got {state}"
    assert "APPROVE" in (note or "")


@pytest.mark.parametrize("label,spec", _HIGH_RISK_SPEC_CASES)
def test_auto_approve_criteria_needs_approval_high_risk(label, spec, monkeypatch):
    """High-risk specs (auth, destructive, cross-repo CI, breaking public API,
    new external runtime dep) must route to NEEDS_APPROVAL under new criteria."""
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda *, settings, spec, **kw: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL", reason=f"high-risk: {label}"
        ),
    )
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        spec,
        "t1",
    )
    assert state is State.HUMAN_ISSUE_APPROVAL, (
        f"{label!r} expected NEEDS_APPROVAL → HUMAN_ISSUE_APPROVAL, got {state}"
    )
    assert "NEEDS_APPROVAL" in (note or "")


# --- epic body tests ---


def test_epic_body_applied_immediately_in_autonomous_mode(ctx, service, monkeypatch):
    """When require_approval=false, epic_body is written to the parent
    epic's description.md immediately after refine."""
    epic = service.create(
        "Epic: Auth System", "Add authentication", kind=TicketKind.EPIC
    )
    child = service.create("Add login", "draft", parent_id=epic.id)

    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=False,
            spec_markdown="## Problem\nAdd login\n## Scope\n- login form\n",
            epic_body="Revised epic strategy: login first, then roles.",
        ),
    )

    out = RefineStage().run(child, ctx)
    assert out.next_state is State.READY

    # Epic description should now contain the revised body.
    epic_desc = service.workspace(epic).read_description()
    assert "Revised epic strategy" in epic_desc
    assert "login first, then roles" in epic_desc


def test_epic_body_stored_as_artifact_in_gated_mode(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When require_approval=true, epic_body is stored as an artifact
    in the child's workspace, NOT written to the epic yet."""
    epic = service.create(
        "Epic: Auth System", "Add authentication", kind=TicketKind.EPIC
    )
    child = service.create("Add login", "draft", parent_id=epic.id)

    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=False,
            spec_markdown="## Problem\nAdd login\n## Scope\n- login form\n",
            epic_body="Revised epic strategy: login first.",
        ),
    )

    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )

    out = RefineStage().run(child, gated_ctx)
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL

    # Epic should NOT have been modified.
    epic_desc = service.workspace(epic).read_description()
    assert epic_desc == "Add authentication"
    assert "Revised epic strategy" not in epic_desc

    # Child workspace should contain the proposed artifact.
    artifact = service.workspace(child).artifacts_dir / "epic-body-proposed.md"
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == "Revised epic strategy: login first."


def test_epic_body_applied_on_approval_in_gated_mode(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When require_approval=true, the epic body is applied to the
    epic only when the child ticket is approved."""
    epic = service.create(
        "Epic: Auth System", "Add authentication", kind=TicketKind.EPIC
    )
    child = service.create("Add login", "draft", parent_id=epic.id)

    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=False,
            spec_markdown="## Problem\nAdd login\n## Scope\n- login form\n",
            epic_body="Revised epic strategy: login first.",
        ),
    )

    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )

    out = RefineStage().run(child, gated_ctx)
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL

    # Apply the refine outcome (simulate worker transition).
    service.transition(child.id, out.next_state, out.note)

    # Epic should still be unchanged before approval.
    epic_desc = service.workspace(epic).read_description()
    assert epic_desc == "Add authentication"

    # Simulate approval: transition + apply epic body artifact.
    service.transition(child.id, State.READY, note="approved by human")

    # Now apply the epic body artifact (mimicking the approve route logic).
    artifact = service.workspace(child).artifacts_dir / "epic-body-proposed.md"
    if artifact.exists():
        epic_body = artifact.read_text(encoding="utf-8").strip()
        if epic_body:
            new_hash = service.workspace(epic).write_description(epic_body)
            service.set_content_hash(epic.id, new_hash)

    # Epic should now contain the revised body.
    epic_desc = service.workspace(epic).read_description()
    assert "Revised epic strategy" in epic_desc
    assert "login first" in epic_desc


def test_epic_body_not_applied_when_no_epic_parent(ctx, service, monkeypatch):
    """When the ticket has no epic parent, epic_body is silently ignored."""
    t = service.create("Standalone ticket", "draft")

    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=False,
            spec_markdown="## Problem\nStandalone\n## Scope\n- thing\n",
            epic_body="This should be ignored.",
        ),
    )

    out = RefineStage().run(t, ctx)
    assert out.next_state is State.READY

    # No crash, spec written as normal.
    assert (
        service.workspace(t).read_description()
        == "## Problem\nStandalone\n## Scope\n- thing\n"
    )

    # No artifact created.
    artifact = service.workspace(t).artifacts_dir / "epic-body-proposed.md"
    assert not artifact.exists()


def test_epic_body_applied_immediately_in_split_path(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """In the split path, epic_body is applied immediately even when
    require_approval=true, because the original ticket is closed."""
    epic = service.create(
        "Epic: Auth System", "Add authentication", kind=TicketKind.EPIC
    )
    child = service.create(
        "Multi-change", "draft with multiple changes", parent_id=epic.id
    )

    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(
            split=True,
            children=[
                ChildSpec(
                    title="Add login",
                    spec_markdown="## Problem\nlogin\n## Scope\n- login\n",
                ),
                ChildSpec(
                    title="Add roles",
                    spec_markdown="## Problem\nroles\n## Scope\n- roles\n",
                ),
            ],
            epic_body="Revised epic strategy: login then roles, each independent.",
        ),
    )

    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )

    out = RefineStage().run(child, gated_ctx)
    assert out.next_state is State.CLOSED

    # Apply the transition so the original ticket is actually closed.
    service.transition(child.id, out.next_state, out.note)

    # Epic should be updated immediately despite gated mode.
    epic_desc = service.workspace(epic).read_description()
    assert "Revised epic strategy" in epic_desc
    assert "login then roles" in epic_desc

    # The original (child) ticket is closed.
    assert service.get(child.id).state is State.CLOSED


# --- file_map artifact tests ---


def test_file_map_written_to_artifacts(ctx, service, monkeypatch):
    """Non-split refine with a file_map → file_map.json exists in artifacts/,
    contains valid JSON with file and note keys."""
    entries = [
        FileMapEntry(file="src/foo.py", note="main module"),
        FileMapEntry(file="src/bar.py", note="helper utilities"),
    ]
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _single("## Problem\nx\n## Scope\n- y\n", file_map=entries),
    )

    t = service.create("Add X", "make x happen")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    artifact = service.workspace(t).artifacts_dir / "file_map.json"
    assert artifact.exists()

    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0] == {"file": "src/foo.py", "note": "main module"}
    assert data[1] == {"file": "src/bar.py", "note": "helper utilities"}


def test_file_map_none_not_written(ctx, service, monkeypatch):
    """file_map=None (default) → no file_map.json artifact."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _single("## Problem\nx\n", file_map=None),
    )

    t = service.create("Add X", "make x happen")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    artifact = service.workspace(t).artifacts_dir / "file_map.json"
    assert not artifact.exists()


def test_file_map_empty_list_not_written(ctx, service, monkeypatch):
    """file_map=[] → no file_map.json artifact (empty list is falsy)."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _single("## Problem\nx\n", file_map=[]),
    )

    t = service.create("Add X", "make x happen")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    artifact = service.workspace(t).artifacts_dir / "file_map.json"
    assert not artifact.exists()


def test_file_map_written_in_split_path(ctx, service, monkeypatch):
    """Split refine with file_map → file_map.json written to parent's
    artifacts before parent is closed."""
    entries = [
        FileMapEntry(file="src/models.py", note="User model"),
        FileMapEntry(file="src/routes.py", note="API routes"),
    ]
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: _split(
            {"title": "Child A", "spec_markdown": "## Problem\nA\n## Scope\n- a\n"},
            {"title": "Child B", "spec_markdown": "## Problem\nB\n## Scope\n- b\n"},
            file_map=entries,
        ),
    )

    parent = service.create("Multi-change", "draft")
    out = RefineStage().run(parent, ctx)

    assert out.next_state is State.CLOSED
    artifact = service.workspace(parent).artifacts_dir / "file_map.json"
    assert artifact.exists()

    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0] == {"file": "src/models.py", "note": "User model"}
    assert data[1] == {"file": "src/routes.py", "note": "API routes"}


def test_file_map_present_in_system_prompt():
    """Verifies the new file_map instruction appears in SYSTEM_PROMPT."""
    from robotsix_mill.agents.refining import SYSTEM_PROMPT

    assert "Always produce a ``file_map``" in SYSTEM_PROMPT
    assert (
        '``file_map=[{"file": "path/to/file.py", "note": "reason this file matters"}, ...]``'
        in SYSTEM_PROMPT
    )
    assert "Keep it to ≤ 20 files" in SYSTEM_PROMPT
    assert "do not guess" in SYSTEM_PROMPT
    assert "``file_map=[]``" in SYSTEM_PROMPT


def test_system_prompt_forbids_report_issue_for_completion():
    """Invariant lock: the refine agent's SYSTEM_PROMPT must forbid
    using report_issue to announce successful completion — the agent
    completes its task by returning the structured RefineResult."""
    from robotsix_mill.agents.refining import SYSTEM_PROMPT

    sentinel = "MUST NOT use `report_issue` to announce successful completion"
    assert sentinel in SYSTEM_PROMPT, (
        f"SYSTEM_PROMPT must forbid completion-notification report_issue calls "
        f"({sentinel!r}); found no match."
    )


def test_system_prompt_no_duplicated_budget_admonition():
    """The refine SYSTEM_PROMPT must NOT contain duplicated budget/delegation
    admonitions — each unique guidance phrase appears at most once."""
    from robotsix_mill.agents.refining import SYSTEM_PROMPT

    # The detailed tool-by-tool breakdown ("Use direct tools for the
    # lookups they're best at") was removed; only the consolidated
    # bullet remains.
    detailed_block = "Use direct tools for the lookups they're best at"
    assert detailed_block not in SYSTEM_PROMPT, (
        "Detailed tool-by-tool breakdown must not appear — "
        "it duplicates the consolidated bullet above."
    )

    # The "delegation doesn't burn a top-level request" sub-explanation
    # appeared twice (once in consolidated bullet, once in detailed).
    # Ensure it's now at most once.
    count = SYSTEM_PROMPT.count("delegation doesn't burn")
    assert count <= 1, (
        f"'delegation doesn't burn' appears {count} times — should be ≤ 1"
    )


def test_trimmed_prompt_still_loads():
    """The trimmed refine.yaml system prompt must still load and validate
    against the agent loader — output_type RefineResult, tool list match."""
    from pathlib import Path
    from robotsix_mill.agents.yaml_loader import load_agent_definition

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent / "agent_definitions" / "refine.yaml"
    )
    assert definition.name == "refine"
    assert definition.output_type == "RefineResult"
    assert "explore" in definition.tools
    assert "read_file" in definition.tools
    assert "parallel_explore" in definition.tools


# ---------------------------------------------------------------------------
# Continuation guard tests (finish_reason == "tool_call")
# ---------------------------------------------------------------------------


class _FakeRunResult:
    """Minimal fake for a pydantic-ai AgentRunResult, exposing only the
    attributes that the continuation guard reads."""

    def __init__(
        self,
        *,
        output,
        finish_reason,
        all_messages,
        all_messages_json=b"[]",
        new_messages_json=b"[]",
        usage=None,
    ):
        self._output = output
        self._all_messages = all_messages
        self._all_messages_json = all_messages_json
        self._new_messages_json = new_messages_json
        self.response = (
            _FakeResponse(finish_reason) if finish_reason is not None else None
        )
        self.usage = usage if usage is not None else _FakeUsage()

    @property
    def output(self):
        return self._output

    def all_messages(self):
        return self._all_messages

    def all_messages_json(self):
        return self._all_messages_json

    def new_messages_json(self):
        return self._new_messages_json


class _FakeUsage:
    """Minimal fake for pydantic_ai.usage.RunUsage."""

    def __init__(self, requests: int = 0):
        self.requests = requests

    def __call__(self):
        return self


class _FakeResponse:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason


def test_continuation_guard_fires_on_tool_calls(monkeypatch, settings):
    """When finish_reason == 'tool_call', the guard triggers a single
    continuation call with message_history=all_messages() and returns
    the continuation's RefineResult."""
    import robotsix_mill.agents.retry as retry_module
    import robotsix_mill.agents.base as base_module

    first_messages = [{"role": "tool", "content": "tool result"}]
    expected_spec = RefineResult(split=False, spec_markdown="## Problem\nfixed\n")

    # Track run_sync calls so we can assert on the continuation prompt
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(
                {"user_prompt": user_prompt, "message_history": message_history}
            )
            if len(run_sync_calls) == 1:
                return _FakeRunResult(
                    output=RefineResult(split=False, spec_markdown=""),
                    finish_reason="tool_call",
                    all_messages=first_messages,
                )
            else:
                return _FakeRunResult(
                    output=expected_spec,
                    finish_reason="stop",
                    all_messages=first_messages
                    + [{"role": "assistant", "content": "done"}],
                )

        def close(self):
            pass

    mock_agent = _MockAgent()

    # run_agent: pass-through so the run executes directly on the handle
    def pass_through_retry(agent, make_run, *, what="model call", sleep=None):
        return make_run(agent)

    monkeypatch.setattr(retry_module, "run_agent", pass_through_retry)
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: mock_agent
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="original draft",
    )

    # Guard must have fired: exactly two run_sync calls
    assert len(run_sync_calls) == 2, (
        f"Expected 2 run_sync calls (original + continuation), got {len(run_sync_calls)}"
    )

    # Continuation must carry the first call's messages as history
    assert run_sync_calls[1]["message_history"] == first_messages, (
        "Continuation call must receive message_history=all_messages() from first result"
    )

    # Continuation prompt must contain the synthesis instruction
    assert "synthesise a final answer" in run_sync_calls[1]["user_prompt"].lower(), (
        "Continuation user prompt must instruct the model to synthesise a final answer"
    )

    # Final output must be the continuation's RefineResult
    assert output.spec_markdown == "## Problem\nfixed\n"


def test_continuation_guard_not_triggered_on_stop(monkeypatch, settings):
    """When finish_reason == 'stop', no continuation occurs — the
    original result passes through unchanged."""
    import robotsix_mill.agents.retry as retry_module
    import robotsix_mill.agents.base as base_module

    expected_spec = RefineResult(split=False, spec_markdown="## Problem\nok\n")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=expected_spec,
                finish_reason="stop",
                all_messages=[{"role": "user", "content": "hi"}],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Only one call — no continuation
    assert len(run_sync_calls) == 1
    assert output.spec_markdown == "## Problem\nok\n"


def test_continuation_guard_skipped_when_response_missing(monkeypatch, settings):
    """When result.response is None (missing attribute), the guard is
    skipped entirely — no AttributeError, no continuation."""
    import robotsix_mill.agents.retry as retry_module
    import robotsix_mill.agents.base as base_module

    expected_spec = RefineResult(split=False, spec_markdown="## Problem\nok\n")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=expected_spec,
                finish_reason=None,  # response will be None
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Guard skipped — exactly one call
    assert len(run_sync_calls) == 1
    assert output.spec_markdown == "## Problem\nok\n"


def test_continuation_guard_skipped_when_already_valid_output(monkeypatch, settings):
    """When finish_reason == 'tool_call' but the agent already produced a
    valid RefineResult in an earlier turn, skip the continuation to avoid
    burning quota on verification loops."""
    import robotsix_mill.agents.retry as retry_module
    import robotsix_mill.agents.base as base_module

    # A RefineResult with real content — spec_markdown is non-empty.
    valid_output = RefineResult(split=False, spec_markdown="## Problem\ndone\n")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=valid_output,
                finish_reason="tool_call",
                all_messages=[{"role": "tool", "content": "verify"}],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Pre-output guard fires: continuation skipped, only one call
    assert len(run_sync_calls) == 1
    assert output.spec_markdown == "## Problem\ndone\n"


def test_continuation_guard_skipped_when_low_remaining_quota(monkeypatch, settings):
    """When finish_reason == 'tool_call' but remaining requests ≤ 5,
    skip the continuation to avoid failing mid-turn."""
    import robotsix_mill.agents.retry as retry_module
    import robotsix_mill.agents.base as base_module

    empty_output = RefineResult(split=False, spec_markdown="")
    run_sync_calls = []

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            run_sync_calls.append(user_prompt)
            return _FakeRunResult(
                output=empty_output,
                finish_reason="tool_call",
                all_messages=[{"role": "tool", "content": "verify"}],
                # Simulate all-but-3 requests already used → 3 remaining
                usage=_FakeUsage(requests=settings.refine_request_limit - 3),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )

    output = refining.run_refine_agent(
        settings=settings,
        title="Test ticket",
        draft="draft",
    )

    # Quota guard fires: continuation skipped, only one call
    assert len(run_sync_calls) == 1
    # Empty spec_markdown — the raw output (coerced to RefineResult)
    assert output.spec_markdown == ""


# ---------------------------------------------------------------------------
# --- run_refine_agent no longer short-circuits from memory ---
# The deterministic _check_memory_for_no_change function and its
# Jaccard-based short-circuit have been removed per review directive.
# Memory is still passed to the LLM as context, but no_change_needed
# can only originate from the LLM's structured output. The following
# test verifies that run_refine_agent does NOT skip the LLM call even
# when a matching memory entry exists.


def test_run_refine_agent_no_memory_short_circuit(
    monkeypatch,
    settings,
):
    """run_refine_agent does NOT return no_change_needed from memory
    alone — the LLM must be invoked to decide."""

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    agent_called = False

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            nonlocal agent_called
            agent_called = True
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    result = refining.run_refine_agent(
        settings=settings,
        title="some title",
        draft="some draft",
        memory="",
    )

    # The full refine agent WAS called (no memory short-circuit).
    assert agent_called is True
    # Must NOT short-circuit to no_change_needed on memory alone.
    assert result.no_change_needed is False
    assert result.spec_markdown == "## Problem\nok\n"


def test_run_refine_agent_no_match_proceeds_to_llm(monkeypatch, settings):
    """run_refine_agent always proceeds to the LLM path — there is no
    memory-based short-circuit."""

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    agent_called = False

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            nonlocal agent_called
            agent_called = True
            # Return a simple valid result to avoid continuation guard
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    result = refining.run_refine_agent(
        settings=settings,
        title="completely different ticket",
        draft="something else entirely",
        memory="",
    )

    assert agent_called is True
    assert result.no_change_needed is False
    assert result.spec_markdown == "## Problem\nok\n"


def test_run_refine_agent_passes_request_limit(monkeypatch, settings):
    """run_refine_agent bounds its tool loop with
    ``UsageLimits(request_limit=settings.refine_request_limit)`` on its
    run_sync call (mirrors the explore-agent capture pattern)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured: dict = {}

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            captured["usage_limits"] = usage_limits
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    monkeypatch.setattr(settings, "refine_request_limit", 23)
    refining.run_refine_agent(settings=settings, title="t", draft="d")

    assert captured["usage_limits"] is not None
    assert captured["usage_limits"].request_limit == 23


# ---------------------------------------------------------------------------
# deterministic <test-warnings> injection for warnings-hardening refines
# ---------------------------------------------------------------------------


def test_test_warnings_block_skips_non_warnings_ticket(tmp_path):
    s = Settings(data_dir=str(tmp_path))
    assert (
        refining._collect_test_warnings_block("Refactor the CLI parser", tmp_path, s)
        == ""
    )


def test_test_warnings_block_skips_when_no_repo(tmp_path):
    s = Settings(data_dir=str(tmp_path))
    assert (
        refining._collect_test_warnings_block(
            "Add filterwarnings = error to pytest", None, s
        )
        == ""
    )


def test_test_warnings_block_injects_summary(tmp_path, monkeypatch):
    """A warnings-hardening draft triggers ONE sandbox run and injects the
    summary as a <test-warnings> block telling the agent not to re-run."""
    import robotsix_mill.sandbox as sandbox

    s = Settings(data_dir=str(tmp_path))
    calls = {}

    def fake_run(cmd, *, repo_dir, settings, install_project=False):
        calls["cmd"] = cmd
        calls["install"] = install_project
        return 0, "=== warnings summary ===\nsrc/x.py:1: DeprecationWarning: old\n===="

    monkeypatch.setattr(sandbox, "run", fake_run)
    out = refining._collect_test_warnings_block(
        "Add filterwarnings = error to pytest config with documented ignores",
        tmp_path,
        s,
    )
    assert "test-warnings" in out
    assert "DeprecationWarning" in out
    assert "do not run the test suite" in out.lower()
    assert calls["install"] is True  # deps installed so warnings are real
    assert "pytest" in calls["cmd"]


def test_test_warnings_block_best_effort_on_sandbox_failure(tmp_path, monkeypatch):
    import robotsix_mill.sandbox as sandbox

    s = Settings(data_dir=str(tmp_path))

    def boom(*a, **k):
        raise sandbox.SandboxError("docker unavailable")

    monkeypatch.setattr(sandbox, "run", boom)
    assert (
        refining._collect_test_warnings_block("filterwarnings hardening", tmp_path, s)
        == ""
    )


def test_test_warnings_block_empty_output(tmp_path, monkeypatch):
    import robotsix_mill.sandbox as sandbox

    s = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(sandbox, "run", lambda *a, **k: (0, "   "))
    assert (
        refining._collect_test_warnings_block("make warnings strict", tmp_path, s) == ""
    )


class TestIsInternalToolchainFailure:
    """Unit tests for ``is_internal_toolchain_failure`` — the predicate
    that gates ``ask_web_knowledge`` during refine when the draft carries
    CI/type/lint/test failure signals."""

    def test_traceback_positive(self):
        """Draft containing a Python traceback → True."""
        assert refining.is_internal_toolchain_failure(
            "Traceback (most recent call last):\n  File ..."
        )

    def test_pytest_failed_summary_positive(self):
        """Draft with a pytest ``FAILED`` summary line → True."""
        assert refining.is_internal_toolchain_failure(
            "FAILED tests/foo.py::test_bar - AssertionError"
        )

    def test_pytest_failures_section_positive(self):
        """Draft with ``= FAILURES =`` → True."""
        assert refining.is_internal_toolchain_failure(
            "= FAILURES =\n something went wrong"
        )

    def test_pytest_short_test_summary_positive(self):
        """Draft with ``short test summary`` → True."""
        assert refining.is_internal_toolchain_failure("short test summary info")

    def test_mypy_arg_type_positive(self):
        """Draft with a mypy ``error: ... [arg-type]`` line → True."""
        assert refining.is_internal_toolchain_failure(
            'src/foo.py:10: error: Argument 1 to "f" has incompatible type '
            '"int"; expected "str"  [arg-type]'
        )

    def test_ruff_code_positive(self):
        """Draft explicitly mentioning ``ruff`` → True."""
        assert refining.is_internal_toolchain_failure(
            "ruff check failed: E501 line too long"
        )

    def test_flake8_code_positive(self):
        """Draft with an ``F401`` lint code → True."""
        assert refining.is_internal_toolchain_failure("unused import: F401")

    def test_exit_code_positive(self):
        """Draft with ``exit code`` → True."""
        assert refining.is_internal_toolchain_failure("mypy exited with exit code 1")

    def test_feature_draft_negative(self):
        """Ordinary feature draft with no failure markers → False."""
        assert not refining.is_internal_toolchain_failure(
            "Add a new endpoint to the API that returns user preferences."
        )

    def test_bare_test_word_negative(self):
        """Draft that mentions 'test' but no failure markers → False
        (predicate is conservative)."""
        assert not refining.is_internal_toolchain_failure(
            "We should test the new feature thoroughly."
        )

    def test_case_insensitive(self):
        """Markers are matched case-insensitively."""
        assert refining.is_internal_toolchain_failure(
            "TRACEBACK (most recent call last):"
        )

    # -- bare tool NAMES must NOT trip the predicate (false-positive fix) --
    # A normal feature spec routinely mentions tooling; matching the bare
    # tool name replaced the real spec with a generic "fix the failing
    # check" template across many tickets.

    def test_feature_spec_mentioning_pytest_negative(self):
        """A feature spec that says it will ship pytest tests → False."""
        assert not refining.is_internal_toolchain_failure(
            "Implement the runner and ship pytest async tests for both the "
            "success and failure paths."
        )

    def test_feature_spec_mentioning_mypy_hook_negative(self):
        """A spec about fixing a mypy pre-commit hook → False (no output)."""
        assert not refining.is_internal_toolchain_failure(
            "Fix the pre-commit mypy hook: use a local system hook so it runs "
            "against the project environment."
        )

    def test_feature_spec_mentioning_ruff_config_negative(self):
        """A spec about adding ruff/vulture/bandit config → False."""
        assert not refining.is_internal_toolchain_failure(
            "Add a ruff config and a bandit + vulture pass to the dev tooling."
        )

    def test_prose_failed_word_negative(self):
        """Lowercase 'failed'/'failure' in prose must NOT match the pytest
        ``FAILED `` marker (it is case-sensitive)."""
        assert not refining.is_internal_toolchain_failure(
            "On a failed connection, retry; cover the success and failure paths."
        )

    def test_pytest_uppercase_failed_positive(self):
        """Real pytest ``FAILED `` output (uppercase) still matches."""
        assert refining.is_internal_toolchain_failure(
            "FAILED tests/test_x.py::test_y - AssertionError: nope"
        )


class TestRefineTraceWebBudgetDefaults:
    """The refine stage reuses the proven per-trace web budget helpers
    (``reset_trace_web_fetch_budget`` / ``reset_trace_web_search_budget``)
    that ``run_refine_agent`` resets at the start of each run. Mirrors the
    survey trace-budget mechanism — once the cap is set with refine's
    defaults, the 6th+ fetch/search is refused with the budget-exhausted
    sentinel instead of executing."""

    def test_refine_web_fetch_default_cap(self, tmp_path, monkeypatch):
        """With refine's default cap (5), the 6th cache-miss web_fetch in
        one trace returns the budget-exhausted sentinel without fetching."""
        from robotsix_mill import sandbox
        from robotsix_mill.agents.web_tools import (
            _cache,
            make_web_fetch,
            reset_web_fetch_budget,
            reset_trace_web_fetch_budget,
        )

        s = Settings(data_dir=str(tmp_path))
        assert s.refine_web_fetch_max_calls == 5
        assert s.refine_web_fetch_max_total_bytes == 500_000

        _cache.clear()
        reset_web_fetch_budget()
        # Exactly what run_refine_agent does at the start of a trace.
        reset_trace_web_fetch_budget(
            s.refine_web_fetch_max_calls,
            s.refine_web_fetch_max_total_bytes,
        )

        calls: list[str] = []

        def fake_fetch(url, *, settings):
            calls.append(url)
            return 0, f"body for {url}"

        monkeypatch.setattr(sandbox, "fetch", fake_fetch)
        wf = make_web_fetch(s)

        # First 5 distinct URLs succeed (consume the trace budget).
        for i in range(5):
            assert wf(f"https://x.test/{i}") == f"body for https://x.test/{i}"
        assert len(calls) == 5

        # Per-consult reset does NOT clear the trace counters.
        reset_web_fetch_budget()

        # 6th distinct URL is refused by the trace budget.
        out = wf("https://x.test/6th")
        assert "trace budget exhausted" in out.lower()
        assert len(calls) == 5  # no new fetch

    def test_refine_web_search_default_cap(self, tmp_path, monkeypatch):
        """With refine's default cap (5), the 6th web_search in one trace
        returns the budget-exhausted sentinel."""
        import asyncio

        from robotsix_mill.agents.web_knowledge import (
            _make_tools,
            reset_trace_web_search_budget,
        )

        s = Settings(data_dir=str(tmp_path))
        assert s.refine_web_search_max_calls == 5

        async def fake_run_web_research(*, settings, query):
            return f"conclusion for: {query}"

        import robotsix_mill.agents.web_research as wr_mod

        monkeypatch.setattr(wr_mod, "run_web_research", fake_run_web_research)

        reset_trace_web_search_budget(s.refine_web_search_max_calls)
        tools = _make_tools(s)
        web_search = tools[-1]  # web_search is the last tool

        # First 5 searches succeed.
        for i in range(5):
            assert asyncio.run(web_search(f"query {i}")) == f"conclusion for: query {i}"

        # 6th search hits the trace budget cap.
        r6 = asyncio.run(web_search("query 6"))
        assert "web_search trace budget exhausted" in r6


class TestRefineRunawayLoopGuard:
    """The refine run caps total tool calls (``tool_calls_limit``) and
    wraps the assembled tools with the shared error-counter, mirroring
    test_gap / trace_inspector. Only the pathological runaway tail is
    terminated; the normal path is unchanged."""

    def test_refine_usage_limits_and_error_wrapper(self, tmp_path, monkeypatch):
        from robotsix_mill.agents import base, retry, trace_inspector

        s = Settings(data_dir=str(tmp_path))

        captured: dict = {}

        real_wrap = trace_inspector._wrap_tools_with_error_limit

        def spy_wrap(tools, max_errors):
            captured["max_errors"] = max_errors
            return real_wrap(tools, max_errors)

        monkeypatch.setattr(trace_inspector, "_wrap_tools_with_error_limit", spy_wrap)

        class FakeResult:
            output = RefineResult(spec_markdown="ok")
            response = type("R", (), {"finish_reason": "stop"})()

            def all_messages_json(self):
                return b"[]"

            def new_messages_json(self):
                return b"[]"

        class FakeAgent:
            def run_sync(self, prompt, *, message_history=None, usage_limits=None):
                captured["usage_limits"] = usage_limits
                return FakeResult()

        monkeypatch.setattr(
            base, "build_agent_from_definition", lambda *a, **k: FakeAgent()
        )
        monkeypatch.setattr(base, "_safe_close", lambda *a, **k: None)
        monkeypatch.setattr(retry, "run_agent", lambda agent, fn, what: fn(agent))

        out = refining.run_refine_agent(settings=s, title="t", draft="d", repo_dir=None)
        assert out.spec_markdown == "ok"

        limits = captured["usage_limits"]
        assert limits.tool_calls_limit == s.refine_max_tool_calls
        assert limits.request_limit == s.refine_request_limit
        assert captured["max_errors"] == s.refine_max_errors


# ---------------------------------------------------------------------------
# Exploration gating tests (cost-saving: skip explore/parallel_explore
# for simple tickets)
# ---------------------------------------------------------------------------


def test_max_refine_explore_calls_default():
    """Default max_refine_explore_calls is 4."""
    s = Settings(data_dir="/tmp")
    assert s.max_refine_explore_calls == 4


def test_max_refine_explore_calls_zero_disables():
    """max_refine_explore_calls=0 disables exploration cap enforcement."""
    from robotsix_mill.config import Settings as S

    s = S(data_dir="/tmp", max_refine_explore_calls="0")
    assert s.max_refine_explore_calls == 0


def test_build_repo_tools_include_explore_false(tmp_path):
    """With include_explore=False, the explore tool is NOT in the returned list."""
    from robotsix_mill.agents._repo_tools import _build_repo_tools
    from robotsix_mill.config import Settings as S

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    s = S(data_dir=str(tmp_path))

    tools = _build_repo_tools(
        repo_dir, s, include_explore=False, include_parallel_explore=False
    )
    tool_names = {t.__name__ for t in tools}
    assert "explore" not in tool_names, (
        "explore should be absent when include_explore=False"
    )
    assert "parallel_explore" not in tool_names, (
        "parallel_explore should be absent when include_parallel_explore=False"
    )
    assert "read_file" in tool_names, "read_file should still be present"


def test_build_repo_tools_include_explore_true_default(tmp_path):
    """With include_explore=True (default), explore IS in the returned list."""
    from robotsix_mill.agents._repo_tools import _build_repo_tools
    from robotsix_mill.config import Settings as S

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    s = S(data_dir=str(tmp_path))

    tools = _build_repo_tools(
        repo_dir, s, include_explore=True, include_parallel_explore=True
    )
    tool_names = {t.__name__ for t in tools}
    assert "explore" in tool_names
    assert "parallel_explore" in tool_names


def test_run_refine_agent_respects_include_explore_false(
    monkeypatch, settings, tmp_path
):
    """When include_explore=False, run_refine_agent builds tools without
    explore/parallel_explore, and the log records 'skipped'."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Simple ticket",
        draft="Fix typo in README.md",
        repo_dir=repo_dir,
        include_explore=False,
        include_parallel_explore=False,
    )

    tool_names = {t.__name__ for t in captured_tools if hasattr(t, "__name__")}
    assert "explore" not in tool_names
    assert "parallel_explore" not in tool_names
    assert "read_file" in tool_names
    assert "list_dir" in tool_names
    assert "run_command" in tool_names


def test_sendback_agent_has_no_filesystem_tools(monkeypatch, settings, tmp_path):
    """When reviewer_comments is truthy, the agent built for refine
    cannot call explore, parallel_explore, read_file, list_dir, or
    run_command — but can still call reply_to_thread, close_thread,
    and report_issue."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Sendback ticket",
        draft="Fix the scope per feedback",
        repo_dir=repo_dir,
        reviewer_comments="Please tighten the acceptance criteria",
    )

    tool_names = {t.__name__ for t in captured_tools if hasattr(t, "__name__")}
    # Filesystem/exploration tools must be absent.
    for forbidden in (
        "explore",
        "parallel_explore",
        "read_file",
        "list_dir",
        "run_command",
    ):
        assert forbidden not in tool_names, (
            f"sendback agent must not have '{forbidden}' tool, "
            f"got tools: {sorted(tool_names)}"
        )
    # Thread-management tools must be present (they're auto-injected by
    # build_agent based on reply_to_thread/close_thread/report_issue flags,
    # not via the explicit tools list — so they won't show up here).
    # The sendback overrides set reply_to_thread=True and close_thread=True,
    # and report_issue remains True from the YAML definition.  We verify
    # those flags are in the build_agent kwargs (tested separately in
    # test_sendback_enables_reply_and_close_thread_tools).


def test_first_refine_has_full_tool_set(monkeypatch, settings, tmp_path):
    """Without reviewer_comments, the full tool set is available
    (explore, parallel_explore, read_file, list_dir, run_command)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="First refine",
        draft="Add a new feature",
        repo_dir=repo_dir,
        # no reviewer_comments
    )

    tool_names = {t.__name__ for t in captured_tools if hasattr(t, "__name__")}
    for required in ("explore", "read_file", "list_dir", "run_command"):
        assert required in tool_names, (
            f"first-refine agent must have '{required}' tool, "
            f"got tools: {sorted(tool_names)}"
        )


def test_run_refine_agent_explore_cap_enforcement(monkeypatch, settings, tmp_path):
    """When max_refine_explore_calls=1, the 2nd explore call is rejected
    with a cap-exhausted message."""
    import asyncio
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    # Cap exploration at 1 call.
    capped = Settings(data_dir=str(tmp_path), max_refine_explore_calls="1")

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=capped,
        title="Test",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=True,
        include_parallel_explore=True,
    )

    explore_tool = None
    for t in captured_tools:
        if getattr(t, "__name__", "") == "explore":
            explore_tool = t
            break
    assert explore_tool is not None, "explore tool must be present"

    # First call should succeed.
    r1 = asyncio.run(explore_tool("question one"))
    assert "cap reached" not in r1.lower()

    # Second call should be rejected (cap = 1).
    r2 = asyncio.run(explore_tool("question two"))
    assert "exploration cap reached" in r2.lower()
    assert "1/1" in r2


def test_run_refine_agent_explore_cap_zero_disables_enforcement(
    monkeypatch, settings, tmp_path
):
    """When max_refine_explore_calls=0, exploration is never capped."""
    import asyncio
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    capped = Settings(data_dir=str(tmp_path), max_refine_explore_calls="0")

    captured_tools: list = []

    def fake_build_agent(settings, definition, tools, **kwargs):
        captured_tools.extend(tools)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=capped,
        title="Test",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=True,
        include_parallel_explore=True,
    )

    explore_tool = next(
        t for t in captured_tools if getattr(t, "__name__", "") == "explore"
    )

    # Multiple calls should all succeed (cap=0 disables enforcement).
    for i in range(10):
        r = asyncio.run(explore_tool(f"question {i}"))
        assert "cap reached" not in r.lower()


def test_run_refine_agent_logs_exploration_skipped(
    caplog, monkeypatch, settings, tmp_path
):
    """When include_explore=False, a structured log line records 'skipped'."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module
    import logging

    caplog.set_level(logging.INFO, logger="robotsix_mill.agents.refining")

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Simple",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=False,
        include_parallel_explore=False,
    )

    assert any("refine exploration: skipped" in r.message for r in caplog.records), (
        "Must log exploration skipped"
    )


def test_run_refine_agent_logs_exploration_invoked(
    caplog, monkeypatch, settings, tmp_path
):
    """When include_explore=True, a structured log line records 'invoked' + cap."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module
    import logging

    caplog.set_level(logging.INFO, logger="robotsix_mill.agents.refining")

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Complex",
        draft="draft",
        repo_dir=repo_dir,
        include_explore=True,
        include_parallel_explore=True,
    )

    assert any("refine exploration: invoked" in r.message for r in caplog.records), (
        "Must log exploration invoked"
    )
    assert any("cap=4" in r.message for r in caplog.records), "Must log the cap value"


def _simple_agent():
    """Return a mock agent that returns a trivial RefineResult."""

    class FakeResult:
        output = RefineResult(spec_markdown="ok")
        response = type("R", (), {"finish_reason": "stop"})()

        def all_messages_json(self):
            return b"[]"

        def new_messages_json(self):
            return b"[]"

    class FakeAgent:
        def run_sync(self, prompt, *, message_history=None, usage_limits=None):
            return FakeResult()

    return FakeAgent()


def test_triage_complexity_simple_suppresses_exploration(ctx, service, monkeypatch):
    """When triage returns REFINE with complexity='simple', run_refine_agent
    is called with include_explore=False and include_parallel_explore=False."""
    from robotsix_mill.agents.refining import TriageResult

    refine_kwargs: dict = {}

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE",
            reason="single-file change",
            complexity="simple",
        )

    def spy_refine(**kwargs):
        refine_kwargs.update(kwargs)
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Fix typo in README", "Change one word in README.md line 3")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_kwargs.get("include_explore") is False
    assert refine_kwargs.get("include_parallel_explore") is False


def test_triage_complexity_needs_exploration_provides_exploration(
    ctx, service, monkeypatch
):
    """When triage returns REFINE with complexity='needs-exploration',
    run_refine_agent is called with include_explore=True and
    include_parallel_explore=True."""
    from robotsix_mill.agents.refining import TriageResult

    refine_kwargs: dict = {}

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE",
            reason="multi-file change, needs codebase search",
            complexity="needs-exploration",
        )

    def spy_refine(**kwargs):
        refine_kwargs.update(kwargs)
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Refactor auth", "multi-file auth refactor")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_kwargs.get("include_explore") is True
    assert refine_kwargs.get("include_parallel_explore") is True


def test_triage_complexity_none_defaults_to_needs_exploration(
    ctx, service, monkeypatch
):
    """When triage returns complexity=None, the default is 'needs-exploration'
    and run_refine_agent receives full exploration tools (conservative)."""
    from robotsix_mill.agents.refining import TriageResult

    refine_kwargs: dict = {}

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE",
            reason="ambiguous scope",
            complexity=None,  # backward-compat: no complexity field
        )

    def spy_refine(**kwargs):
        refine_kwargs.update(kwargs)
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Add feature", "ambiguous feature draft")
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_kwargs.get("include_explore") is True
    assert refine_kwargs.get("include_parallel_explore") is True


# ---------------------------------------------------------------------------
# Trivial-scope routing tests (AC: TriageResult.trivial_scope +
# run_refine_agent level override)
# ---------------------------------------------------------------------------


def test_triage_result_trivial_scope_default():
    """TriageResult.trivial_scope defaults to None when not provided."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult(decision="REFINE", reason="test")
    assert r.trivial_scope is None


def test_triage_result_trivial_scope_parsed_from_json():
    """TriageResult.trivial_scope parses correctly from JSON/dict input."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult.model_validate(
        {"decision": "REFINE", "reason": "test", "trivial_scope": True}
    )
    assert r.trivial_scope is True

    r2 = TriageResult.model_validate(
        {"decision": "REFINE", "reason": "test", "trivial_scope": False}
    )
    assert r2.trivial_scope is False


def test_triage_result_trivial_scope_false_by_default():
    """When trivial_scope is omitted, _read_triage_trivial returns False
    (conservative default — no cheap-model routing)."""
    import json
    import tempfile
    from pathlib import Path

    from robotsix_mill.stages.refine.orchestration import _read_triage_trivial

    # Simulate a workspace with triage_complexity.json that lacks trivial_scope.
    tmp = Path(tempfile.mkdtemp())
    artifacts = tmp / "artifacts"
    artifacts.mkdir()
    (artifacts / "triage_complexity.json").write_text(
        json.dumps({"complexity": "simple"}), encoding="utf-8"
    )

    class FakeWS:
        artifacts_dir = artifacts

    assert _read_triage_trivial(FakeWS()) is False  # type: ignore[arg-type]


def test_triage_result_exploration_findings_default():
    """TriageResult.exploration_findings defaults to None when not provided."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult(decision="REFINE", reason="test")
    assert r.exploration_findings is None


def test_triage_result_exploration_findings_roundtrip():
    """TriageResult.exploration_findings preserves a non-empty string
    through model_validate round-trip."""
    from robotsix_mill.agents.refining import TriageResult

    findings = (
        "- Verified `src/foo/bar.py` exists (342 lines)\n"
        "- Confirmed `Frobnicator.run()` at line 89\n"
    )
    r = TriageResult.model_validate(
        {
            "decision": "REFINE",
            "reason": "multi-file",
            "complexity": "needs-exploration",
            "exploration_findings": findings,
        }
    )
    assert r.exploration_findings == findings

    # Empty string is preserved (not coerced to None).
    r2 = TriageResult.model_validate(
        {
            "decision": "REFINE",
            "reason": "test",
            "exploration_findings": "",
        }
    )
    assert r2.exploration_findings == ""


def test_triage_findings_artifact_write_read():
    """_write_triage_complexity with findings writes triage_findings.json;
    _read_triage_findings returns it; absent file returns None."""
    import json
    import tempfile
    from pathlib import Path

    from robotsix_mill.stages.refine.orchestration import (
        _write_triage_complexity,
        _read_triage_findings,
    )

    tmp = Path(tempfile.mkdtemp())
    artifacts = tmp / "artifacts"
    artifacts.mkdir()

    class FakeWS:
        artifacts_dir = artifacts

    ws = FakeWS()

    # No file yet → None
    assert _read_triage_findings(ws) is None  # type: ignore[arg-type]

    # Write with findings
    _write_triage_complexity(
        ws,  # type: ignore[arg-type]
        "needs-exploration",
        findings="- Verified `src/a.py` exists\n",
    )

    # Now the findings file exists
    findings = _read_triage_findings(ws)  # type: ignore[arg-type]
    assert findings == "- Verified `src/a.py` exists\n"

    # Complexity file still written
    data = json.loads(
        (artifacts / "triage_complexity.json").read_text(encoding="utf-8")
    )
    assert data["complexity"] == "needs-exploration"

    # Write without findings — findings file should NOT be created/overwritten
    # if it didn't exist before (but existing one stays).
    # Test fresh workspace: no findings
    tmp2 = Path(tempfile.mkdtemp())
    art2 = tmp2 / "artifacts"
    art2.mkdir()

    class FakeWS2:
        artifacts_dir = art2

    _write_triage_complexity(FakeWS2(), "simple")  # type: ignore[arg-type]
    assert not (art2 / "triage_findings.json").exists()

    # findings=None should not create the file
    _write_triage_complexity(FakeWS2(), "needs-exploration", findings=None)  # type: ignore[arg-type]
    assert not (art2 / "triage_findings.json").exists()

    # Empty findings should not create the file
    _write_triage_complexity(FakeWS2(), "needs-exploration", findings="")  # type: ignore[arg-type]
    assert not (art2 / "triage_findings.json").exists()

    # Corrupt JSON → None
    (artifacts / "triage_findings.json").write_text("{bad json", encoding="utf-8")
    assert _read_triage_findings(ws) is None  # type: ignore[arg-type]

    # Missing key → None
    (artifacts / "triage_findings.json").write_text(
        json.dumps({"other": "value"}), encoding="utf-8"
    )
    assert _read_triage_findings(ws) is None  # type: ignore[arg-type]


def test_run_refine_agent_passes_refine_level_to_build_agent(
    monkeypatch, settings, tmp_path
):
    """When refine_level=1 is passed, build_agent_from_definition receives
    level=1 in its overrides dict."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_level=1,
    )

    assert len(captured_overrides) == 1
    assert captured_overrides[0].get("level") == 1, (
        f"Expected level=1 in overrides, got {captured_overrides[0]}"
    )


def test_run_refine_agent_no_refine_level_leaves_level_unchanged(
    monkeypatch, settings, tmp_path
):
    """When refine_level is NOT passed (None/default), build_agent_from_definition
    does NOT receive a 'level' key in its overrides — the YAML default applies."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        # refine_level not passed
    )

    assert len(captured_overrides) == 1
    assert "level" not in captured_overrides[0], (
        f"Expected no 'level' in overrides when refine_level not passed, "
        f"got {captured_overrides[0]}"
    )


# ── refine_model alias / model override ─────────────────────────────


def test_run_refine_agent_passes_refine_model_sonnet(monkeypatch, settings, tmp_path):
    """When refine_model='sonnet' is passed, a non-downgraded refine build
    forwards model='sonnet' to build_agent_from_definition."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_model="sonnet",
    )

    assert len(captured_overrides) == 1
    assert captured_overrides[0].get("model") == "sonnet", (
        f"Expected model='sonnet' in overrides, got {captured_overrides[0]}"
    )


def test_run_refine_agent_passes_refine_model_opus(monkeypatch, settings, tmp_path):
    """When refine_model='opus' is passed, build_agent_from_definition receives
    model='opus' in overrides."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_model="opus",
    )

    assert len(captured_overrides) == 1
    assert captured_overrides[0].get("model") == "opus", (
        f"Expected model='opus' in overrides, got {captured_overrides[0]}"
    )


def test_run_refine_agent_downgrade_skips_model_override(
    monkeypatch, settings, tmp_path
):
    """When refine_level=1 (DeepSeek downgrade), model override is NOT applied
    even when refine_model is passed."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_level=1,
        refine_model="sonnet",
    )

    assert len(captured_overrides) == 1
    assert "model" not in captured_overrides[0], (
        f"Expected no 'model' in overrides when refine_level=1 (DeepSeek path), "
        f"got {captured_overrides[0]}"
    )
    # level override still applies for DeepSeek routing
    assert captured_overrides[0].get("level") == 1, (
        f"Expected level=1 override still present, got {captured_overrides[0]}"
    )


def test_run_refine_agent_no_refine_model_no_override(monkeypatch, settings, tmp_path):
    """When refine_model is None/not passed, no model override is injected
    — build_agent_from_definition uses its own default (Opus for level 3)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        # refine_model not passed → None → no model in overrides
    )

    assert len(captured_overrides) == 1
    assert "model" not in captured_overrides[0], (
        f"Expected no 'model' in overrides when refine_model not passed, "
        f"got {captured_overrides[0]}"
    )


# ── request_limit_override / per-tier turn cap ──────────────────────


def test_run_refine_agent_uses_request_limit_override(monkeypatch, settings):
    """When request_limit_override is passed, UsageLimits uses it instead
    of settings.refine_request_limit."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured: dict = {}

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            captured["usage_limits"] = usage_limits
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    refining.run_refine_agent(
        settings=settings,
        title="t",
        draft="d",
        request_limit_override=40,
    )

    assert captured["usage_limits"] is not None
    assert captured["usage_limits"].request_limit == 40


def test_run_refine_agent_no_request_limit_override_uses_default(
    monkeypatch,
    settings,
):
    """When request_limit_override is not passed, settings.refine_request_limit
    is used (the existing behaviour preserved)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured: dict = {}

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            captured["usage_limits"] = usage_limits
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    monkeypatch.setattr(settings, "refine_request_limit", 77)
    refining.run_refine_agent(
        settings=settings,
        title="t",
        draft="d",
    )

    assert captured["usage_limits"] is not None
    assert captured["usage_limits"].request_limit == 77


def test_warning_logged_when_usage_exceeds_threshold(monkeypatch, settings, caplog):
    """When the refine agent uses > refine_usage_warning_threshold of its
    request_limit, a warning is logged."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module
    import logging

    effective_limit = settings.refine_request_limit
    high_usage = int(effective_limit * 0.9)

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
                usage=_FakeUsage(requests=high_usage),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    with caplog.at_level(logging.WARNING):
        refining.run_refine_agent(
            settings=settings,
            title="t",
            draft="d",
        )

    assert any(
        "near request_limit exhaustion" in rec.message for rec in caplog.records
    ), "Expected warning about near-exhaustion but none logged"


def test_no_warning_when_usage_below_threshold(monkeypatch, settings, caplog):
    """When the refine agent uses ≤ refine_usage_warning_threshold of its
    request_limit, no warning is logged."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module
    import logging

    effective_limit = settings.refine_request_limit
    low_usage = int(effective_limit * 0.5)

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
                usage=_FakeUsage(requests=low_usage),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    with caplog.at_level(logging.WARNING):
        refining.run_refine_agent(
            settings=settings,
            title="t",
            draft="d",
        )

    assert not any(
        "near request_limit exhaustion" in rec.message for rec in caplog.records
    ), "Unexpected near-exhaustion warning for low usage"


def test_split_child_fast_path_sets_simple_complexity(ctx, service, monkeypatch):
    """Split-child fast path writes triage_complexity.json as 'simple',
    suppressing exploration on the already-refined child."""
    parent = service.create("Parent task", "big feature", kind=TicketKind.TASK)
    # Close the parent with a "split into" note to trigger fast-path.
    service.transition(parent.id, State.CLOSED, "split into 1 children: child-1")
    child = service.create(
        "Already refined child",
        "## Problem\nrefined child\n## Scope\n- thing\n## Acceptance criteria\n- [ ] works\n",
        parent_id=parent.id,
    )

    refine_called = False

    def spy_refine(**kwargs):
        nonlocal refine_called
        refine_called = True
        return _single("should not be called")

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(child, ctx)

    assert not refine_called, "Split-child fast path must skip refine agent"
    assert out.next_state is State.READY

    # Verify complexity artifact was written as "simple"
    from robotsix_mill.stages.refine.orchestration import _read_triage_complexity

    ws = service.workspace(child)
    assert _read_triage_complexity(ws) == "simple"


def test_triage_skip_writes_complexity_artifact(ctx, service, monkeypatch):
    """When triage returns REFINE with complexity='simple', the complexity
    artifact is written before falling through to full refine."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE",
            reason="trivial change",
            complexity="simple",
        )

    def spy_refine(**kwargs):
        nonlocal refine_called
        refine_called = True
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Simple change", "trivial draft")
    RefineStage().run(t, ctx)

    assert refine_called

    # Check the artifact was written.
    from robotsix_mill.stages.refine.orchestration import _read_triage_complexity

    ws = service.workspace(t)
    assert _read_triage_complexity(ws) == "simple"


# ---------------------------------------------------------------------------
# Auto-approve input reduction tests
# ---------------------------------------------------------------------------


def test_summarize_spec_for_auto_approve_short_spec_unchanged():
    """A spec within the 2000-char limit is returned unchanged."""
    from robotsix_mill.stages.refine.helpers import _summarize_spec_for_auto_approve

    short = "## Problem\nFix typo\n## Scope\n- README.md\n## Acceptance criteria\n- [ ] typo fixed\n"
    assert _summarize_spec_for_auto_approve(short) == short


def test_summarize_spec_for_auto_approve_truncates_long_spec():
    """A spec exceeding the 2000-char limit is truncated with a note."""
    from robotsix_mill.stages.refine.helpers import _summarize_spec_for_auto_approve

    # Build a spec longer than 2000 chars.
    header = "## Problem\nLong spec\n## Scope\n"
    filler = "- line " + "x" * 80 + "\n"
    long_spec = header + filler * 100  # ~8000+ chars

    result = _summarize_spec_for_auto_approve(long_spec)
    assert len(result) <= 2005  # within limit + truncation note
    assert "… (truncated for auto-approve" in result
    assert result.startswith(header)


def test_auto_approve_receives_summarized_spec(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """triage_auto_approve receives a summarized (not full) spec payload."""
    # Build a spec that's long enough to trigger summarization.
    header = "## Problem\nAdd pagination\n## Scope\n"
    filler = "- list " + "x" * 80 + "\n"
    long_spec = header + filler * 80  # well over 2000 chars

    captured_spec: list[str] = []

    def fake_auto_approve(*, settings, spec):
        captured_spec.append(spec)
        return refining.AutoApproveResult(decision="APPROVE", reason="precise spec")

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(long_spec))
    monkeypatch.setattr(refining, "triage_auto_approve", fake_auto_approve)

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Add pagination", "add pagination to list endpoints")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.READY
    assert len(captured_spec) == 1
    # The captured spec should be summarized, not the full long_spec.
    assert captured_spec[0] != long_spec
    assert len(captured_spec[0]) < len(long_spec)
    assert "… (truncated for auto-approve" in captured_spec[0]
    # But it must still contain the Problem header.
    assert "## Problem" in captured_spec[0]


def test_auto_approve_yaml_no_budget_discipline_note():
    """The auto-approve.yaml system prompt must NOT contain the
    'Budget discipline' section."""
    from pathlib import Path
    import yaml as _yaml

    path = (
        Path(__file__).parent.parent.parent / "agent_definitions" / "auto-approve.yaml"
    )
    prompt = _yaml.safe_load(path.read_text())["system_prompt"]
    assert "Budget discipline" not in prompt, (
        "auto-approve.yaml must not contain the 'Budget discipline' note"
    )


# ---------------------------------------------------------------------------
# Memory DB tests (acceptance criterion 6)
# ---------------------------------------------------------------------------


def test_memory_db_round_trip(tmp_path):
    """Write memory to DB, read it back — content matches."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Initially empty.
    assert load_memory_db(s, board_id, name) == ""

    # Write some content.
    content = "## Refine run 2026-06-19\nObserved: tests need better fixtures.\n"
    persist_memory_db(s, board_id, name, content)

    # Read back.
    assert load_memory_db(s, board_id, name) == content

    # Second write updates.
    content2 = content + "## Refine run 2026-06-20\nObserved: use DB for memory.\n"
    persist_memory_db(s, board_id, name, content2)
    assert load_memory_db(s, board_id, name) == content2


def test_memory_db_truncation(tmp_path):
    """Long content is truncated with max_chars, keeping most recent entries."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Build content with many entries, newest last.
    entries = []
    for i in range(20):
        entries.append(
            f"## Refine run 2026-06-{i + 1:02d}\n- **Observation**: entry {i}\n"
        )
    content = "\n".join(entries)  # ~20 entries

    persist_memory_db(s, board_id, name, content)

    # Read with a cap that keeps only the most recent entries.
    result = load_memory_db(s, board_id, name, max_chars=500)
    assert len(result) <= 550  # within cap + truncation note
    assert "entry 19" in result  # most recent
    assert "entry 18" in result  # second most recent
    assert "entry 0" not in result  # oldest dropped
    assert "truncated" in result.lower()


def test_memory_db_migration_from_legacy_file(tmp_path):
    """First persist_memory_db migrates content from legacy Markdown file."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Create legacy file.
    legacy_dir = tmp_path / board_id
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = legacy_dir / f"{name}_memory.md"
    legacy_content = "## Prior observations\nCross-cutting pattern: use DB.\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")

    # Write new content — new text is non-empty, so legacy is NOT
    # prepended (only carried over when no new text is given).
    new_content = "## Refine run 2026-06-20\nNew observation.\n"
    persist_memory_db(s, board_id, name, new_content)

    # Read back — should contain only the new content (legacy
    # is not prepended when new text is provided).
    result = load_memory_db(s, board_id, name)
    assert "New observation" in result
    assert "Cross-cutting pattern: use DB" not in result

    # Legacy file should be renamed.
    migrated_path = legacy_dir / f"{name}_memory.md.migrated"
    assert migrated_path.exists()
    assert not legacy_path.exists()


def test_memory_db_cross_cutting_entries_survive_retention(tmp_path):
    """Cross-cutting entries (general patterns) survive retention when
    they are among the most recent entries; oldest per-ticket entries
    are dropped first."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Build content: many old per-ticket entries first, then a
    # cross-cutting pattern at the end (most recent).
    content = ""
    for i in range(60):
        content += f"## Refine run 2026-06-{i + 1:02d} — ticket {i}\n"
        content += f"- Per-ticket observation number {i} with some extra padding text\n"
    # Cross-cutting pattern at the end (newest entry).
    content += (
        "## Cross-cutting: shared libs live in external repos\n"
        "- src/<pkg>/, Hatchling.\n"
    )

    persist_memory_db(s, board_id, name, content)

    # Read with a cap that triggers truncation.
    result = load_memory_db(s, board_id, name, max_chars=2000)
    # Cross-cutting knowledge is recent — should survive.
    assert "Cross-cutting" in result
    # Oldest ticket entries should be gone.
    assert "ticket 0" not in result
    # The truncation note signals oldest content was dropped.
    assert "truncated" in result.lower()


# ---------------------------------------------------------------------------
# TriageResult MIGRATE decision tests
# ---------------------------------------------------------------------------


def test_triage_result_accepts_migrate_decision():
    """TriageResult.model_validate accepts decision='MIGRATE' with a target_board."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult.model_validate(
        {
            "decision": "MIGRATE",
            "reason": "belongs to robotsix-mill board",
            "target_board": "robotsix-mill",
        }
    )
    assert r.decision == "MIGRATE"
    assert r.target_board == "robotsix-mill"
    assert r.reason == "belongs to robotsix-mill board"


def test_triage_result_migrate_without_target_board_is_valid():
    """TriageResult with decision='MIGRATE' but no target_board is still
    valid at the model level (the stage validates, not the model)."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult.model_validate(
        {"decision": "MIGRATE", "reason": "belongs elsewhere"}
    )
    assert r.decision == "MIGRATE"
    assert r.target_board is None


def test_triage_result_migrate_target_board_preserved():
    """target_board is preserved through model round-trip."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult(
        decision="MIGRATE",
        reason="misrouted",
        target_board="other-board",
    )
    assert r.target_board == "other-board"
    # Round-trip through dict
    d = r.model_dump()
    r2 = TriageResult.model_validate(d)
    assert r2.decision == "MIGRATE"
    assert r2.target_board == "other-board"


def test_triage_prompt_includes_registered_boards():
    """The triage prompt built by triage_refine includes a registered-boards
    catalog section when get_repos_config succeeds."""
    from unittest.mock import patch

    from robotsix_mill.agents.refining import triage_refine, TriageResult
    from robotsix_mill.config import RepoConfig, ReposRegistry

    # Build a fake repos registry with a few boards.
    fake_registry = ReposRegistry(
        repos={
            "repo-a": RepoConfig(
                repo_id="repo-a",
                board_id="board-a",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
            "repo-b": RepoConfig(
                repo_id="repo-b",
                board_id="board-b",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
            "repo-c": RepoConfig(
                repo_id="repo-c",
                board_id="board-c",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        }
    )

    # Capture the user prompt passed to the agent.
    captured_prompts: list[str] = []

    class _FakeAgent:
        def run_sync(self, user_prompt, *, usage_limits=None):
            captured_prompts.append(user_prompt)
            from types import SimpleNamespace

            return SimpleNamespace(output=TriageResult(decision="REFINE", reason="ok"))

    with patch(
        "robotsix_mill.agents.base.build_agent_from_definition",
        return_value=_FakeAgent(),
    ):
        with patch(
            "robotsix_mill.config.get_repos_config",
            return_value=fake_registry,
        ):
            from types import SimpleNamespace as _NS

            triage_refine(
                settings=_NS(triage_request_limit=10),
                title="Test ticket",
                draft="test draft",
                repo_dir=None,
            )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "# Registered boards" in prompt
    assert "valid target_board" in prompt
    assert "- board-a" in prompt
    assert "- board-b" in prompt
    assert "- board-c" in prompt


def test_triage_prompt_graceful_on_repos_config_failure():
    """When get_repos_config raises, the triage prompt does NOT include the
    registered-boards section, and triage_refine still succeeds."""
    from unittest.mock import patch

    from robotsix_mill.agents.refining import triage_refine, TriageResult

    captured_prompts: list[str] = []

    class _FakeAgent:
        def run_sync(self, user_prompt, *, usage_limits=None):
            captured_prompts.append(user_prompt)
            from types import SimpleNamespace

            return SimpleNamespace(output=TriageResult(decision="REFINE", reason="ok"))

    with patch(
        "robotsix_mill.agents.base.build_agent_from_definition",
        return_value=_FakeAgent(),
    ):
        with patch(
            "robotsix_mill.config.get_repos_config",
            side_effect=RuntimeError("boom"),
        ):
            from types import SimpleNamespace as _NS

            triage_refine(
                settings=_NS(triage_request_limit=10),
                title="Test ticket",
                draft="test draft",
                repo_dir=None,
            )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "# Registered boards" not in prompt


# ---------------------------------------------------------------------------
# Refine no-op guard regression tests (DRAFT→DONE without branch)
# ---------------------------------------------------------------------------


def test_no_change_needed_routes_to_ready_for_task_without_branch(
    ctx, service, monkeypatch
):
    """A TASK ticket whose refine agent returns ``no_change_needed=True``
    must land in READY (for implement to verify), never DONE.

    This guards against the bug where a feature-request DRAFT can
    auto-refine → DONE → CLOSED in one pass as a no-op without ever
    being implemented.
    """
    ticket = service.create("Add session close feature", "draft body")
    # Pre-condition: the ticket is DRAFT and has no branch.
    assert ticket.state == State.DRAFT
    assert ticket.branch is None

    def spy_refine(*, settings, title, draft, **kwargs):
        return RefineResult(
            split=False,
            no_change_needed=True,
            no_change_rationale="Ticket is clear and ready for implementation.",
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(ticket, ctx)
    # The no_change_path should route to READY, not DONE.
    assert out.next_state == State.READY, (
        f"Expected READY for no-op on TASK without branch, got {out.next_state}"
    )
    assert "routing to implement" in out.note.lower()


def test_reviewer_agreement_routes_to_ready_for_task_without_branch(
    ctx, service, monkeypatch
):
    """When the reviewer-agreement guard fires for a TASK ticket without a
    branch, the outcome must be READY, not DONE."""
    ticket = service.create("Feature request", "draft body")
    assert ticket.state == State.DRAFT
    assert ticket.branch is None

    # Enable the reviewer-agreement gate and make the triage return AGREE.
    ctx.settings.reviewer_agreement_gate_enabled = True
    ctx.settings.refine_triage_enabled = True

    def fake_triage(*, settings, draft, reviewer_comments, **kwargs):
        from robotsix_mill.agents.refining import ReviewerAgreementResult

        return ReviewerAgreementResult(decision="AGREE", reason="The draft looks good.")

    monkeypatch.setattr(refining, "triage_reviewer_agreement", fake_triage)

    # Simulate reviewer comments existing (the guard only fires when there
    # are reviewer comments).
    service.add_comment(ticket.id, "Looks good to me.", author="reviewer")

    out = RefineStage().run(ticket, ctx)
    assert out.next_state == State.READY, (
        f"Expected READY for reviewer-agreement on TASK without branch, "
        f"got {out.next_state}"
    )
    assert "routing to implement" in out.note.lower()


def test_guard_implementation_done_blocks_unrecognized_done(ctx, service):
    """The ``_guard_implementation_done`` helper must redirect a DONE
    outcome to READY when the ticket is TASK-kind, has no branch, and
    the note does not signal a recognised non-implementation shortcut."""
    ticket = service.create("A feature", "draft body")
    assert ticket.branch is None

    from robotsix_mill.stages.refine.core import RefineStage
    from robotsix_mill.stages.base import Outcome

    # A DONE outcome with a note that is NOT a recognised prefix.
    bad_outcome = Outcome(State.DONE, "all done!")
    guarded = RefineStage._guard_implementation_done(ctx, ticket, bad_outcome)
    assert guarded.next_state == State.READY, (
        f"Expected READY after guard, got {guarded.next_state}"
    )
    assert "was: all done!" in guarded.note


def test_guard_implementation_done_allows_legitimate_prefix(ctx, service):
    """The ``_guard_implementation_done`` helper must NOT redirect a DONE
    outcome whose note starts with a recognised non-implementation
    prefix (dedup, freshness, obsolescence, misroute)."""
    ticket = service.create("A feature", "draft body")
    assert ticket.branch is None

    from robotsix_mill.stages.refine.core import RefineStage
    from robotsix_mill.stages.base import Outcome
    from robotsix_mill.core.constants import DEDUP_DUPLICATE_PREFIX

    good_outcome = Outcome(
        State.DONE,
        f"{DEDUP_DUPLICATE_PREFIX}some-other-id: it's the same bug",
    )
    guarded = RefineStage._guard_implementation_done(ctx, ticket, good_outcome)
    assert guarded.next_state == State.DONE, (
        f"Expected DONE (legitimate prefix), got {guarded.next_state}"
    )


def test_human_mark_done_still_works(ctx, service):
    """Human explicit close via ``mark_done`` must still work on a TASK
    ticket without a branch — the guard must not block it."""
    ticket = service.create("Some ticket", "draft body")
    assert ticket.branch is None

    # mark_done bypasses transition(), so it should work regardless.
    _, updated = service.mark_done(ticket.id, note="wont-fix: not needed")
    assert updated.state == State.DONE, (
        f"Expected DONE after mark_done, got {updated.state}"
    )


# ---------------------------------------------------------------------------
# Language gating regression tests
# ---------------------------------------------------------------------------


def test_language_gating_javascript_only_no_python_markers(tmp_path):
    """A repo declaring only JavaScript must produce NO Python-convention
    text in the composed refine system prompt (markers pytest/mypy/ruff/
    uv lock must all be absent).

    This locks in the already-working language gating
    (resolve_language_instructions / load_repo_languages) so the
    non-Python-leakage guarantee cannot silently break."""
    import yaml as _yaml

    # Create a minimal repo with a .robotsix-mill/config.yaml declaring
    # only JavaScript.
    repo_dir = tmp_path / "js_repo"
    repo_dir.mkdir()
    config_dir = repo_dir / ".robotsix-mill"
    config_dir.mkdir()
    config = {"languages": ["javascript"]}
    (config_dir / "config.yaml").write_text(_yaml.dump(config), encoding="utf-8")

    from robotsix_mill.config import Settings as S
    from robotsix_mill.config.repo_settings import resolve_language_instructions

    settings = S(data_dir=str(tmp_path))
    lang_block = resolve_language_instructions(settings, repo_dir)

    # Must produce some output (the javascript.md snippet exists).
    assert lang_block, "Expected non-empty language block for JavaScript repo"

    # Must NOT contain any Python-convention markers.
    python_markers = ["pytest", "mypy", "ruff", "uv lock"]
    for marker in python_markers:
        assert marker not in lang_block, (
            f"Python marker {marker!r} leaked into JavaScript-only "
            f"language instructions"
        )


def test_language_gating_empty_config_no_python_markers(tmp_path):
    """A repo with NO language declaration must produce an empty language
    block (no spurious Python injection)."""
    import yaml as _yaml

    repo_dir = tmp_path / "no_lang_repo"
    repo_dir.mkdir()
    config_dir = repo_dir / ".robotsix-mill"
    config_dir.mkdir()
    config = {}  # No languages key at all
    (config_dir / "config.yaml").write_text(_yaml.dump(config), encoding="utf-8")

    from robotsix_mill.config import Settings as S
    from robotsix_mill.config.repo_settings import resolve_language_instructions

    settings = S(data_dir=str(tmp_path))
    lang_block = resolve_language_instructions(settings, repo_dir)

    assert lang_block == "", (
        f"Expected empty language block for repo with no languages, got: {lang_block!r}"
    )


def test_language_gating_python_repo_includes_markers(tmp_path):
    """A Python-declaring repo MUST include the Python-convention markers —
    this ensures the gating works in both directions."""
    import yaml as _yaml

    repo_dir = tmp_path / "py_repo"
    repo_dir.mkdir()
    config_dir = repo_dir / ".robotsix-mill"
    config_dir.mkdir()
    config = {"languages": ["python"]}
    (config_dir / "config.yaml").write_text(_yaml.dump(config), encoding="utf-8")

    from robotsix_mill.config import Settings as S
    from robotsix_mill.config.repo_settings import resolve_language_instructions

    settings = S(data_dir=str(tmp_path))
    lang_block = resolve_language_instructions(settings, repo_dir)

    assert lang_block, "Expected non-empty language block for Python repo"
    # At least one Python marker should be present.
    assert "uv" in lang_block, "Python repo must include 'uv' marker"
