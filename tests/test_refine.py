import hashlib

import pytest

from robotsix_mill.agents import dedup
from robotsix_mill.agents import refining
from robotsix_mill.config import Settings
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage
from robotsix_mill.runtime.worker import process_ticket


@pytest.fixture(autouse=True)
def _dedup_clean(monkeypatch):
    """All pre-existing tests expect the dedup guard to be a no-op
    (novel draft).  Dedup-specific tests override this fixture."""
    monkeypatch.setattr(
        dedup, "run_dedup_check",
        lambda **_: {"duplicate_of": None, "already_done": None, "reason": "no match"},
    )


@pytest.fixture
def ctx(settings, service):
    return StageContext(settings=settings, service=service)


def test_empty_draft_blocks(ctx, service):
    t = service.create("x", "   ")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty draft" in out.note


def test_no_api_key_blocks(ctx, service, monkeypatch):
    def boom(*, settings, title, draft, repo_dir=None):
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    monkeypatch.setattr(refining, "run_refine_agent", boom)
    out = RefineStage().run(service.create("x", "do a thing"), ctx)
    assert out.next_state is State.BLOCKED
    assert "OPENROUTER_API_KEY" in out.note


def test_success_rewrites_description(ctx, service, monkeypatch):
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: spec
    )
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    ws = service.workspace(t)
    assert ws.read_description() == spec
    assert (ws.artifacts_dir / "draft-original.md").read_text() == "make x happen"
    # DB pointer kept in sync with the rewritten file
    expected = hashlib.sha256(spec.encode("utf-8")).hexdigest()
    assert service.get(t.id).content_hash == expected


def test_empty_spec_blocks(ctx, service, monkeypatch):
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: "  \n ")
    out = RefineStage().run(service.create("x", "draft"), ctx)
    assert out.next_state is State.BLOCKED


async def test_chains_draft_to_implement(ctx, service, monkeypatch):
    """Full wiring: emit -> refine -> ready -> implement. Implement is
    real but no FORGE_REMOTE_URL, so the chain halts at BLOCKED there —
    proving draft never needs a manual transition."""
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: "## Problem\nspec\n"
    )
    t = service.create("Add X", "rough idea")

    await process_ticket(t.id, ctx)

    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    states = [e.state for e in service.history(t.id)]
    assert State.READY in states  # refine ran and advanced it
    assert "FORGE_REMOTE_URL" in service.history(t.id)[-1].note


# --- approval gate tests ---


def test_refine_goes_to_awaiting_approval_when_gated(ctx, service, monkeypatch, tmp_path):
    """When require_approval=true, refine transitions to awaiting_approval."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)
    gated_settings = Settings(
        MILL_DATA_DIR=str(tmp_path), MILL_REQUIRE_APPROVAL="true"
    )
    gated_ctx = StageContext(settings=gated_settings, service=service)
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.AWAITING_APPROVAL
    assert service.get(t.id).state is State.DRAFT  # worker hasn't applied transition


def test_refine_goes_to_ready_when_autonomous(ctx, service, monkeypatch):
    """When require_approval=false, refine transitions to ready (autonomous)."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY


async def test_awaiting_approval_pauses_chain(ctx, service, monkeypatch):
    """When require_approval=true, the worker pauses at awaiting_approval
    (no stage owns it), so the ticket is not picked up by implement."""
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: "## Problem\nspec\n"
    )
    t = service.create("Add X", "rough idea")
    # apply refine outcome with gated settings
    from robotsix_mill.config import Settings as S
    gated = S(MILL_DATA_DIR=str(ctx.settings.data_dir), MILL_REQUIRE_APPROVAL="true")
    gated_ctx = StageContext(settings=gated, service=service)
    outcome = RefineStage().run(t, gated_ctx)
    service.transition(t.id, outcome.next_state, outcome.note)

    # now the ticket is in awaiting_approval — worker should stop here
    await process_ticket(t.id, gated_ctx)

    reloaded = service.get(t.id)
    assert reloaded.state is State.AWAITING_APPROVAL
    # worker didn't advance past awaiting_approval
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

    def fake_refine(*, settings, title, draft, repo_dir=None):
        seen["repo_dir"] = repo_dir
        return "## Problem\nx\n## Scope\n- y\n"

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    t = service.create("x", "do a thing")
    RefineStage().run(t, ctx)
    repo = service.workspace(t).dir / "repo"
    assert seen["clone"] == 1
    assert seen["repo_dir"] == repo            # agent got the local clone

    # second run: clone already present -> reused, not re-cloned
    service.create  # noqa - keep ref
    seen["clone"] = 0
    t2 = service.get(t.id)
    RefineStage().run(t2, ctx)
    assert seen["clone"] == 0
    assert seen["repo_dir"] == repo


def test_refine_clone_failure_falls_back_to_draft_only(ctx, service, monkeypatch):
    import subprocess

    from robotsix_mill.vcs import git_ops

    ctx.settings.forge_remote_url = "https://example.test/repo.git"
    got = {}

    def boom_clone(url, dest, branch, token):
        raise subprocess.CalledProcessError(128, "git", stderr="no access")

    def fake_refine(*, settings, title, draft, repo_dir=None):
        got["repo_dir"] = repo_dir
        return "## Problem\nx\n"

    monkeypatch.setattr(git_ops, "clone", boom_clone)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)
    out = RefineStage().run(service.create("x", "do a thing"), ctx)
    assert out.next_state in (State.AWAITING_APPROVAL, State.READY)
    assert got["repo_dir"] is None             # degraded to draft-only


def test_web_fetch_confined_to_web_research_subagent():
    """Invariant lock: raw web_fetch is wired ONLY inside the
    web_research sub-agent (which summarises); no other agent exposes
    it. (web_tools.py is the definition module.)"""
    from pathlib import Path

    import robotsix_mill.agents as ap

    offenders = [
        f.name for f in Path(ap.__file__).parent.glob("*.py")
        if "make_web_fetch" in f.read_text()
        and f.name not in ("web_research.py", "web_tools.py")
    ]
    assert offenders == [], f"web_fetch leaked into: {offenders}"


# --- dedup guard tests ---


def test_dedup_duplicate_ticket_closes(ctx, service, monkeypatch):
    """Exact-duplicate draft → CLOSED. Refine agent is never called."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)

    t_a = service.create("Add dark mode toggle", "Add dark mode toggle.")
    t_b = service.create("Add dark mode toggle", "Add dark mode toggle.")

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        return {
            "duplicate_of": t_a.id,
            "already_done": None,
            "reason": "same change",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(*, settings, title, draft, repo_dir=None):
        nonlocal refine_called
        refine_called = True
        return orig_refine(settings=settings, title=title, draft=draft, repo_dir=repo_dir)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t_b, ctx)

    assert out.next_state is State.CLOSED
    assert f"duplicate of {t_a.id}" in out.note
    assert "same change" in out.note
    assert not refine_called


def test_dedup_already_committed_closes(ctx, service, monkeypatch):
    """Already-committed draft → CLOSED. Refine agent not called."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)

    t = service.create("Add X", "make x happen")

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        return {
            "duplicate_of": None,
            "already_done": "abc1234",
            "reason": "change in commit",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(*, settings, title, draft, repo_dir=None):
        nonlocal refine_called
        refine_called = True
        return orig_refine(settings=settings, title=title, draft=draft, repo_dir=repo_dir)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "already implemented in abc1234" in out.note
    assert "change in commit" in out.note
    assert not refine_called


def test_dedup_novel_draft_proceeds_normally(ctx, service, monkeypatch):
    """Novel draft → refine runs normally, transitions to READY."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)

    t = service.create("Add X", "make x happen")

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        return {
            "duplicate_of": None,
            "already_done": None,
            "reason": "no match",
        }

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(*, settings, title, draft, repo_dir=None):
        nonlocal refine_called
        refine_called = True
        return orig_refine(settings=settings, title=title, draft=draft, repo_dir=repo_dir)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_dedup_skipped_for_empty_draft(ctx, service, monkeypatch):
    """Empty draft blocks BEFORE dedup check — no wasted model call."""
    dedup_called = False

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(service.create("x", "   "), ctx)
    assert out.next_state is State.BLOCKED
    assert "empty draft" in out.note
    assert not dedup_called


def test_dedup_never_flags_self(ctx, service, monkeypatch):
    """Candidate list passed to dedup must NOT contain the current ticket's id."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)

    t = service.create("my ticket", "my draft")
    # Create another ticket so the candidate list isn't empty
    service.create("other ticket", "other draft")

    seen_candidates = None

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        nonlocal seen_candidates
        import json
        seen_candidates = json.loads(candidates_json)
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    RefineStage().run(t, ctx)

    assert seen_candidates is not None
    candidate_ids = [c["id"] for c in seen_candidates]
    assert t.id not in candidate_ids


def test_dedup_failure_degrades_gracefully(ctx, service, monkeypatch):
    """Dedup check raises → refine proceeds normally."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)

    t = service.create("Add X", "make x happen")

    def boom_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        raise RuntimeError("dedup model down")

    monkeypatch.setattr(dedup, "run_dedup_check", boom_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(*, settings, title, draft, repo_dir=None):
        nonlocal refine_called
        refine_called = True
        return orig_refine(settings=settings, title=title, draft=draft, repo_dir=repo_dir)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_dedup_no_forge_passes_none_commits(ctx, service, monkeypatch):
    """No forge → dedup called with recent_commits_json=None, no crash."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)

    t = service.create("Add X", "make x happen")

    seen_commits = "unset"

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        nonlocal seen_commits
        seen_commits = recent_commits_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    # No forge_remote_url set — repo_dir stays None
    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert seen_commits is None


def test_dedup_clone_failure_passes_none_commits(ctx, service, monkeypatch):
    """Clone fails → dedup called with recent_commits_json=None, no crash."""
    import subprocess

    from robotsix_mill.vcs import git_ops

    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: spec)

    ctx.settings.forge_remote_url = "https://example.test/repo.git"
    seen_commits = "unset"

    def boom_clone(url, dest, branch, token):
        raise subprocess.CalledProcessError(128, "git", stderr="no access")

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        nonlocal seen_commits
        seen_commits = recent_commits_json
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(git_ops, "clone", boom_clone)
    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state in (State.AWAITING_APPROVAL, State.READY)
    assert seen_commits is None


def test_draft_to_closed_transition_is_legal():
    """DRAFT → CLOSED is a valid transition in the state machine."""
    from robotsix_mill.core.states import can_transition
    from robotsix_mill.core.states import State as S

    assert can_transition(S.DRAFT, S.CLOSED) is True
