import hashlib
from pathlib import Path

import pytest

from robotsix_mill.agents import dedup
from robotsix_mill.agents import refining
from robotsix_mill.agents.kb import load_kb
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


def test_empty_title_and_draft_blocks(ctx, service):
    t = service.create("   ", "   ")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty title and draft" in out.note


def test_no_api_key_blocks(ctx, service, monkeypatch):
    def boom(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
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
        *, settings, title, draft, repo_dir=None, reviewer_comments=None
    ):
        nonlocal refine_called
        refine_called = True
        assert title == "Add dark mode toggle"
        assert draft == ""
        return {"split": False, "spec": spec}

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
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec}
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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": "  \n "})
    out = RefineStage().run(service.create("x", "draft"), ctx)
    assert out.next_state is State.BLOCKED


async def test_chains_draft_to_implement(ctx, service, monkeypatch):
    """Full wiring: emit -> refine -> ready -> implement. Implement is
    real but no FORGE_REMOTE_URL, so the chain halts at BLOCKED there —
    proving draft never needs a manual transition."""
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: {"split": False, "spec": "## Problem\nspec\n"}
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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})
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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY


async def test_awaiting_approval_pauses_chain(ctx, service, monkeypatch):
    """When require_approval=true, the worker pauses at awaiting_approval
    (no stage owns it), so the ticket is not picked up by implement."""
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: {"split": False, "spec": "## Problem\nspec\n"}
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

    def fake_refine(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
        seen["repo_dir"] = repo_dir
        return {"split": False, "spec": "## Problem\nx\n## Scope\n- y\n"}

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

    def fake_refine(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
        got["repo_dir"] = repo_dir
        return {"split": False, "spec": "## Problem\nx\n"}

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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

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

    def spy_refine(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
        nonlocal refine_called
        refine_called = True
        return orig_refine(settings=settings, title=title, draft=draft, repo_dir=repo_dir)

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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

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

    def spy_refine(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
        nonlocal refine_called
        refine_called = True
        return orig_refine(settings=settings, title=title, draft=draft, repo_dir=repo_dir)

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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

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

    def spy_refine(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
        nonlocal refine_called
        refine_called = True
        return orig_refine(settings=settings, title=title, draft=draft, repo_dir=repo_dir)

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert refine_called


def test_dedup_skipped_for_empty_title_and_draft(ctx, service, monkeypatch):
    """When both title and draft are empty, blocks BEFORE dedup check."""
    dedup_called = False

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        nonlocal dedup_called
        dedup_called = True
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(service.create("", "   "), ctx)
    assert out.next_state is State.BLOCKED
    assert "empty title and draft" in out.note
    assert not dedup_called


def test_dedup_runs_for_title_only(ctx, service, monkeypatch):
    """When title is set but body is empty, dedup IS called (not skipped)."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

    dedup_called = False

    def fake_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        nonlocal dedup_called
        dedup_called = True
        assert draft_title == "Add dark mode toggle"
        assert draft_body == ""
        return {"duplicate_of": None, "already_done": None, "reason": "no match"}

    monkeypatch.setattr(dedup, "run_dedup_check", fake_dedup)

    out = RefineStage().run(service.create("Add dark mode toggle", ""), ctx)
    assert out.next_state is State.READY
    assert dedup_called


def test_dedup_never_flags_self(ctx, service, monkeypatch):
    """Candidate list passed to dedup must NOT contain the current ticket's id."""
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

    t = service.create("Add X", "make x happen")

    def boom_dedup(*, settings, draft_title, draft_body,
                   candidates_json, recent_commits_json):
        raise RuntimeError("dedup model down")

    monkeypatch.setattr(dedup, "run_dedup_check", boom_dedup)

    refine_called = False
    orig_refine = refining.run_refine_agent

    def spy_refine(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

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
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec})

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


def test_dedup_guard_survives_preexisting_closed_ticket(
    ctx, service, monkeypatch
):
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
        refining, "run_refine_agent", lambda **_: {"split": False, "spec": "## Problem\nspec\n"}
    )
    t = service.create("Add Y", "rough idea")
    out = RefineStage().run(t, ctx)  # must NOT raise TypeError
    assert out.next_state is not State.ERRORED


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
    cutoff = datetime.fromtimestamp(
        now.timestamp() - 30 * 86400, tz=tz.utc
    )
    assert ticket.updated_at >= cutoff  # must not raise TypeError
    assert ticket.created_at >= cutoff


# --- KB injection into refine agent ---


def test_refine_agent_sees_kb_content(monkeypatch, tmp_path):
    """When kb_dir contains entries, the refine agent's system prompt
    includes the KB content."""
    from robotsix_mill.agents import base as base_mod

    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "gotcha.md").write_text("# Test Gotcha\n\nA known limitation.\n")

    seen_system_prompt: list[str] = []
    seen_name: list = []

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_system_prompt.append(system_prompt)
        seen_name.append(kwargs.get("name"))
        class FakeAgent:
            def run_sync(self, msg):
                return type("R", (), {"output": "## Problem\nok\n"})()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(MILL_DATA_DIR=str(tmp_path), MILL_KB_DIR=str(kb_dir))
    result = refining.run_refine_agent(
        settings=s, title="Test", draft="draft",
    )

    assert result == {"split": False, "spec": "## Problem\nok"}
    assert len(seen_system_prompt) == 1
    prompt = seen_system_prompt[0]
    assert "# Technology Constraints" in prompt
    assert "Test Gotcha" in prompt
    assert "A known limitation" in prompt
    assert seen_name == ["refine"]


def test_refine_agent_no_kb_when_dir_missing(monkeypatch, tmp_path):
    """When kb_dir doesn't exist, the system prompt is unchanged
    (no KB section injected)."""
    from robotsix_mill.agents import base as base_mod

    seen_system_prompt: list[str] = []

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        seen_system_prompt.append(system_prompt)
        class FakeAgent:
            def run_sync(self, msg):
                return type("R", (), {"output": "## Problem\nok\n"})()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    missing = tmp_path / "nonexistent_kb"
    s = Settings(MILL_DATA_DIR=str(tmp_path), MILL_KB_DIR=str(missing))
    result = refining.run_refine_agent(
        settings=s, title="Test", draft="draft",
    )

    assert result == {"split": False, "spec": "## Problem\nok"}
    assert len(seen_system_prompt) == 1
    prompt = seen_system_prompt[0]
    assert "# Technology Constraints" not in prompt


def test_refine_agent_kb_dir_default(tmp_path):
    """settings.kb_dir defaults to Path('kb')."""
    s = Settings(MILL_DATA_DIR=str(tmp_path))
    assert s.kb_dir == Path("kb")


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
            def run_sync(self, msg):
                return type("R", (), {"output": "## Problem\nok\n"})()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s, title="Test", draft="draft", repo_dir=repo,
    )

    assert result == {"split": False, "spec": "## Problem\nok"}
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
            def run_sync(self, msg):
                return type("R", (), {"output": "## Problem\nok\n"})()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s, title="Test", draft="draft", repo_dir=None,
    )

    assert result == {"split": False, "spec": "## Problem\nok"}
    assert seen_tools == []  # no fs tools when no repo


# --- split detection tests ---


def test_split_creates_children_and_closes_parent(ctx, service, monkeypatch):
    """Multi-scope draft → N child tickets created, parent CLOSED."""
    child_a_spec = "## Problem\nAdd checksum verification\n## Scope\n- verify checksums\n"
    child_b_spec = "## Problem\nAdd HEALTHCHECK\n## Scope\n- add HEALTHCHECK\n"

    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "Add checksum verification", "spec_markdown": child_a_spec, "depends_on": []},
                {"title": "Add HEALTHCHECK", "spec_markdown": child_b_spec, "depends_on": [0]},
            ],
        },
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

    # Both children exist and have correct parent_id.
    child_a = service.get(ids_in_note[0])
    child_b = service.get(ids_in_note[1])
    assert child_a is not None
    assert child_b is not None
    assert child_a.parent_id == parent.id
    assert child_b.parent_id == parent.id

    # Children have the right state (READY by default, no require_approval).
    assert child_a.state is State.READY
    assert child_b.state is State.READY

    # Children have the refined spec in their workspace.
    assert service.workspace(child_a).read_description().rstrip("\n") == child_a_spec.rstrip("\n")
    assert service.workspace(child_b).read_description().rstrip("\n") == child_b_spec.rstrip("\n")

    # Child B depends on child A.
    from robotsix_mill.core.service import _parse_depends_on_str
    assert _parse_depends_on_str(child_b.depends_on) == [child_a.id]

    # Child A has no dependencies.
    assert _parse_depends_on_str(child_a.depends_on) == []


def test_split_depends_on_indices_map_correctly(ctx, service, monkeypatch):
    """depends_on zero-based indices resolve to real child ticket IDs."""
    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "Task 1", "spec_markdown": "## Problem\n1\n## Scope\n- one\n", "depends_on": []},
                {"title": "Task 2", "spec_markdown": "## Problem\n2\n## Scope\n- two\n", "depends_on": [0]},
                {"title": "Task 3", "spec_markdown": "## Problem\n3\n## Scope\n- three\n", "depends_on": [0, 1]},
            ],
        },
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
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "The only change", "spec_markdown": child_spec, "depends_on": []},
            ],
        },
    )

    t = service.create("Single change", "just one thing")
    out = RefineStage().run(t, ctx)

    # Should NOT be CLOSED — fallback to normal single-spec path.
    assert out.next_state is State.READY
    assert "single child" in out.note

    # Description should be the child's spec (not the original draft).
    assert service.workspace(t).read_description().rstrip("\n") == child_spec.rstrip("\n")
    # Title should be updated to child's title.
    assert service.get(t.id).title == "The only change"

    # draft-original.md preserved.
    assert (service.workspace(t).artifacts_dir / "draft-original.md").exists()


def test_split_empty_children_blocks(ctx, service, monkeypatch):
    """No valid children → BLOCKED."""
    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {"split": True, "children": []},
    )

    t = service.create("Empty split", "draft")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED


def test_split_malformed_children_skipped(ctx, service, monkeypatch):
    """Malformed child entries (missing title, missing spec) are skipped;
    if only one survives, fall back to single-spec."""
    good_spec = "## Problem\nGood\n## Scope\n- good\n"
    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "", "spec_markdown": "## Problem\nBad\n", "depends_on": []},  # no title
                {"title": "Good", "spec_markdown": good_spec, "depends_on": []},
                {"title": "Bad", "spec_markdown": "", "depends_on": []},  # no spec
                "not-a-dict",  # wrong type
            ],
        },
    )

    t = service.create("Mixed children", "draft")
    out = RefineStage().run(t, ctx)

    # Only "Good" survives → fallback to single-spec.
    assert out.next_state is State.READY
    assert "single child" in out.note
    assert service.workspace(t).read_description().rstrip("\n") == good_spec.rstrip("\n")


def test_split_require_approval_honoured_per_child(ctx, service, monkeypatch, tmp_path):
    """When require_approval=true, children go to AWAITING_APPROVAL."""
    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "Child A", "spec_markdown": "## Problem\nA\n## Scope\n- a\n", "depends_on": []},
                {"title": "Child B", "spec_markdown": "## Problem\nB\n## Scope\n- b\n", "depends_on": []},
            ],
        },
    )

    gated_settings = Settings(
        MILL_DATA_DIR=str(tmp_path), MILL_REQUIRE_APPROVAL="true"
    )
    gated_ctx = StageContext(settings=gated_settings, service=service)

    parent = service.create("Gated split", "draft")
    out = RefineStage().run(parent, gated_ctx)

    assert out.next_state is State.CLOSED
    ids_in_note = out.note.replace("split into ", "").split(", ")
    assert len(ids_in_note) == 2

    for cid in ids_in_note:
        child = service.get(cid)
        assert child.state is State.AWAITING_APPROVAL, f"{cid} should be awaiting_approval"


def test_split_child_skips_re_refinement(ctx, service, monkeypatch):
    """A split child's refine stage short-circuits: no agent call, uses existing spec."""
    child_a_spec = "## Problem\nAlready refined A\n## Scope\n- done a\n"
    child_b_spec = "## Problem\nAlready refined B\n## Scope\n- done b\n"

    # Step 1: Create a parent and split it into TWO children (need 2+ to trigger actual split).
    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "Child A", "spec_markdown": child_a_spec, "depends_on": []},
                {"title": "Child B", "spec_markdown": child_b_spec, "depends_on": []},
            ],
        },
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
    with core_db.session(service.settings) as s:
        t = s.get(TicketModel, child_a_id)
        t.state = State.DRAFT
        t.blocked_from = None
        s.add(t)
        s.commit()

    # Step 3: Now run RefineStage on child A — it should skip the agent.
    refine_called = False

    def spy_refine(*, settings, title, draft, repo_dir=None, reviewer_comments=None):
        nonlocal refine_called
        refine_called = True
        return {"split": False, "spec": draft}

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    child = service.get(child_a_id)
    assert child.state is State.DRAFT
    assert child.parent_id == parent.id

    out2 = RefineStage().run(child, ctx)

    # Should NOT have called the refine agent.
    assert not refine_called
    # Should transition to READY (no require_approval).
    assert out2.next_state is State.READY
    assert "split child" in out2.note

    # The description should still be the original refined spec.
    assert service.workspace(child).read_description().rstrip("\n") == child_a_spec.rstrip("\n")


def test_split_preserves_parent_draft_original(ctx, service, monkeypatch):
    """Parent's draft-original.md is preserved when splitting."""
    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "Child 1", "spec_markdown": "## Problem\n1\n## Scope\n- one\n", "depends_on": []},
                {"title": "Child 2", "spec_markdown": "## Problem\n2\n## Scope\n- two\n", "depends_on": []},
            ],
        },
    )

    parent = service.create("Parent ticket", "original multi-change draft")
    RefineStage().run(parent, ctx)

    draft_original = service.workspace(parent).artifacts_dir / "draft-original.md"
    assert draft_original.exists()
    assert draft_original.read_text() == "original multi-change draft"


def test_split_with_invalid_depends_on_indices_handled(ctx, service, monkeypatch):
    """depends_on indices that are out of range or point to future children are ignored."""
    monkeypatch.setattr(
        refining, "run_refine_agent",
        lambda **_: {
            "split": True,
            "children": [
                {"title": "Task A", "spec_markdown": "## Problem\nA\n## Scope\n- a\n", "depends_on": [5]},  # out of range
                {"title": "Task B", "spec_markdown": "## Problem\nB\n## Scope\n- b\n", "depends_on": [0]},  # valid
                {"title": "Task C", "spec_markdown": "## Problem\nC\n## Scope\n- c\n", "depends_on": [-1, 0]},  # negative ignored, 0 valid
            ],
        },
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
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: {"split": False, "spec": spec}
    )
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    ws = service.workspace(t)
    assert ws.read_description() == spec
    assert (ws.artifacts_dir / "draft-original.md").read_text() == "make x happen"
    expected = hashlib.sha256(spec.encode("utf-8")).hexdigest()
    assert service.get(t.id).content_hash == expected


def test_refine_agent_fallback_raw_markdown(monkeypatch, tmp_path):
    """When the agent outputs raw Markdown (no JSON envelope), it is
    treated as a single-scope spec (graceful fallback)."""
    from robotsix_mill.agents import base as base_mod

    raw_md = "## Problem\nraw output\n## Scope\n- no json"

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        class FakeAgent:
            def run_sync(self, msg):
                return type("R", (), {"output": raw_md})()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s, title="Test", draft="draft",
    )

    assert result == {"split": False, "spec": raw_md}


def test_refine_agent_malformed_json_fallback(monkeypatch, tmp_path):
    """When the agent outputs something that looks like a JSON envelope
    but is malformed, fall back to raw-Markdown treatment."""
    from robotsix_mill.agents import base as base_mod

    raw = '{"split": false, "spec": "## Problem\nunclosed string'

    def fake_build_agent(settings, system_prompt, tools, web, model_name, **kwargs):
        class FakeAgent:
            def run_sync(self, msg):
                return type("R", (), {"output": raw})()
        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    result = refining.run_refine_agent(
        settings=s, title="Test", draft="draft",
    )

    # Falls back to raw-as-spec.
    assert result == {"split": False, "spec": raw}
