import hashlib
import json
from pathlib import Path

import pytest

from robotsix_mill.agents import dedup
from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import ChildSpec, FileMapEntry, RefineResult
from robotsix_mill.config import Settings
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage
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
    """Clone failure escalates to BLOCKED. The diagnostic and remediation
    hint land in the transition note (history) rather than a comment —
    v1 moved agent conclusions out of comments so comments stay
    reserved for ASK_USER + review threads."""
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
    ):
        refine_called.append(True)
        return _single("## Problem\nx\n")

    monkeypatch.setattr(git_ops, "clone", boom_clone)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)
    t = service.create("x", "do a thing")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "refine clone failed" in (out.note or "")
    assert "resume-blocked" in (out.note or "")
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
    """Clone failure escalates to BLOCKED before dedup runs at all —
    no half-grounded refine attempts."""
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

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
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
    epic_a = service.create("Epic A: Agent Memory", "memory system", kind="epic")
    # Epic B — unrelated
    epic_b = service.create(
        "Epic B: Deploy Config", "deployment things", kind="epic"
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
        "Epic C: Observability", "metrics", kind="epic"
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
    epic = service.create("Some epic", "stuff", kind="epic")
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_system_prompt.append(system_prompt)

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_tools.extend(t.__name__ for t in tools)

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_tools.extend(t.__name__ for t in tools)

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
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
    assert seen_tools == []  # no fs tools when no repo


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
    epics = [t for t in all_tickets if t.kind == "epic"]
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
    ):
        nonlocal refine_called
        refine_called = True
        return _single(draft)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    child = service.get(child_a_id)
    assert child.state is State.DRAFT
    # Child should be parented to the umbrella epic, not the original parent.
    all_tickets = service.list()
    epics = [t for t in all_tickets if t.kind == "epic"]
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_system_prompt.append(system_prompt)

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_system_prompt.append(system_prompt)

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
                return type("R", (), {"output": _single("## Problem\nok\n")})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    refining.run_refine_agent(settings=s, title="Test", draft="draft")

    assert len(seen_system_prompt) == 1
    prompt = seen_system_prompt[0]
    # The old "## Tool strategy" section has been moved out of the
    # refine agent's SYSTEM_PROMPT and into ToolRegistry.describe_for_prompt(),
    # which is injected by _compose_prompt().  Because this test
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_system_prompt.append(system_prompt)

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
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

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_system_prompt.append(system_prompt)

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
                return type("R", (), {"output": _single("## Problem\nok\n")})()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    refining.run_refine_agent(settings=s, title="Test", draft="draft")

    assert len(seen_system_prompt) == 1
    assert seen_system_prompt[0] == SYSTEM_PROMPT


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


# --- epic context -------------------------------------------------------


def test_epic_context_passed_to_refine_agent(ctx, service, monkeypatch):
    """When a ticket has an epic parent, epic_context is passed to
    run_refine_agent and contains the epic description."""
    epic = service.create("Global Epic", "High-level: unify UX", kind="epic")
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
    parent = service.create("Parent task", "Ordinary task", kind="task")
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
            lambda **_: RefineResult(
                split=False, spec_markdown="## Problem\nx\n", title=empty_title
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
    """triage_refine builds an agent with zero tools, web=False, and
    the correct triage_model."""
    from robotsix_mill.agents import base as base_mod
    from robotsix_mill.agents.refining import triage_refine, TriageResult

    seen_kwargs: dict = {}

    def fake_build_agent(
        settings,
        system_prompt,
        output_type,
        tools,
        web,
        report_issue,
        model_name,
        name,
        **kwargs,
    ):
        seen_kwargs.update(
            tools=tools,
            web=web,
            report_issue=report_issue,
            model_name=model_name,
            name=name,
        )

        class FakeAgent:
            def run_sync(self, msg, message_history=None, board_id=""):
                return type(
                    "R", (), {"output": TriageResult(decision="REFINE", reason="test")}
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path), triage_model="test/triage-model")
    result = triage_refine(settings=s, title="Test", draft="do x in foo.py")

    assert result.decision == "REFINE"
    assert seen_kwargs["tools"] == []
    assert seen_kwargs["web"] is False
    assert seen_kwargs["report_issue"] is False
    assert seen_kwargs["model_name"] == "test/triage-model"
    assert seen_kwargs["name"] == "triage"


def test_triage_skip_skips_full_refine(ctx, service, monkeypatch):
    """When triage returns SKIP, run_refine_agent is NOT called,
    the draft is preserved, and the ticket goes to READY."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False

    def fake_triage(*, settings, title, draft):
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
    ):
        nonlocal refine_called
        refine_called = True
        return _single("should not be called")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    t = service.create(
        "Update README", "Change the version badge in `docs/README.md` line 5."
    )
    out = RefineStage().run(t, ctx)

    assert not refine_called
    assert out.next_state is State.READY
    assert "triage SKIP:" in out.note
    assert "doc-only change" in out.note


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

    def fake_triage(*, settings, title, draft):
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

    def fake_triage(*, settings, title, draft):
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

    def fake_triage(*, settings, title, draft):
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


# --- epic body tests ---


def test_epic_body_applied_immediately_in_autonomous_mode(ctx, service, monkeypatch):
    """When require_approval=false, epic_body is written to the parent
    epic's description.md immediately after refine."""
    epic = service.create("Epic: Auth System", "Add authentication", kind="epic")
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
    epic = service.create("Epic: Auth System", "Add authentication", kind="epic")
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
    epic = service.create("Epic: Auth System", "Add authentication", kind="epic")
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
    epic = service.create("Epic: Auth System", "Add authentication", kind="epic")
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
