import hashlib

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    RefineResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import process_ticket
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage
from tests.agents.conftest import (
    _single,
)


def test_empty_title_and_draft_blocks(ctx, service):
    t = service.create("   ", "   ")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty title and draft" in out.note


def test_dep_gated_ticket_is_deferred_not_cancelled(ctx, service, monkeypatch):
    """A DRAFT ticket with an unmet dependency is deferred (same-state
    no-op, no LLM spend) — NEVER short-circuited to DONE, which is
    terminal and silently cancels the child (2026-09-02 incident)."""
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


def test_empty_spec_gated_goes_to_approval_not_blocked(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """Whitespace-only spec + gated + usable draft → HUMAN_ISSUE_APPROVAL.

    Previously asserted BLOCKED. That was the behaviour of the "never
    auto-approve an empty spec" guard, which the degenerate-spec paths hit by
    resolving from a literal "" — and it made this the largest blocked class
    on the live board.

    BLOCKED is the wrong destination here and is self-perpetuating: resuming a
    blocked ticket re-runs refine, which produces the same empty spec, which
    blocks it again. HUMAN_ISSUE_APPROVAL is the ordinary gated route — the
    same one a successfully refined ticket takes under `require_approval` — so
    the draft can be approved and move on.

    The guard's actual intent is preserved: the ticket is still NOT
    auto-approved to READY.
    """
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single("  \n "))
    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )
    t = service.create("x", "draft")
    out = RefineStage().run(t, gated_ctx)
    assert out.next_state is not State.READY  # never auto-approved
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "no usable spec" in out.note
    assert service.workspace(t).read_description() == "draft"


def test_empty_spec_and_empty_draft_gated_still_blocks(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """Whitespace-only spec + gated + degenerate draft → BLOCKED.

    With nothing usable in either the spec or the draft there is genuinely
    nothing to implement from, so blocking remains correct. This is the half
    of the guard that must survive the fix above.
    """
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single("  \n "))
    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )
    t = service.create("x", "TBD")
    out = RefineStage().run(t, gated_ctx)
    assert out.next_state is State.BLOCKED
    assert "original draft is empty too" in out.note


async def test_chains_draft_to_implement(ctx, service, monkeypatch):
    """Full wiring: emit -> refine -> ready -> implement. Implement is
    real but no forge_remote_url, so the chain halts at BLOCKED there —
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
    assert "forge_remote_url" in service.history(t.id)[-1].note


# ---------------------------------------------------------------------------
# Refine no-op guard regression tests (DRAFT→DONE without branch)
# ---------------------------------------------------------------------------


def test_no_change_needed_routes_to_done_for_task_without_branch(
    ctx, service, monkeypatch
):
    """A TASK ticket whose refine agent returns ``no_change_needed=True``
    must land in DONE (skipping implement), since refine already determined
    no code changes are needed.

    This was historically guarded to READY for TASK tickets without a
    branch, but the guard was wasting ~$0.13 and ~2286s across 2 tickets
    by running implement on tickets refine already determined needed no
    code change.
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
    # The no_change_path should route to DONE, skipping implement.
    assert out.next_state == State.DONE, (
        f"Expected DONE for no-op on TASK without branch, got {out.next_state}"
    )
    assert "no change needed" in out.note.lower()


def test_no_change_blocked_when_draft_demands_code_change(ctx, service, monkeypatch):
    """When the draft explicitly mandates a non-empty diff, refine must
    NOT close as DONE via the no_change path — it must degrade to the
    normal refine path (→ READY for implement) instead."""
    ticket = service.create(
        "Wire real token exchange",
        "## Acceptance criteria\n\n"
        "- The implementation must produce a non-empty diff.\n"
        "- The ticket must not be closed without changes.",
    )
    assert ticket.state == State.DRAFT
    assert ticket.branch is None

    def spy_refine(*, settings, title, draft, **kwargs):
        return RefineResult(
            split=False,
            no_change_needed=True,
            no_change_rationale="The feature is already implemented.",
        )

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(ticket, ctx)
    # Must NOT be DONE — the draft mandates a non-empty diff, so the
    # no_change conclusion is contradictory.
    assert out.next_state != State.DONE, (
        f"Expected non-DONE outcome when draft demands code change, "
        f"got {out.next_state} — {out.note}"
    )


def test_reviewer_agreement_routes_to_ready_for_task_without_branch(
    ctx, service, monkeypatch
):
    """When the reviewer-agreement guard fires for a TASK ticket without a
    branch, the outcome must be READY, not DONE."""
    ticket = service.create("Feature request", "draft body")
    assert ticket.state == State.DRAFT
    assert ticket.branch is None

    # Enable the reviewer-agreement gate and make the pre-refine classifier
    # return AGREE.
    ctx.settings.reviewer_agreement_gate_enabled = True
    ctx.settings.refine_triage_enabled = True

    from robotsix_mill.agents.pre_refine_classifier import (
        PreRefineClassifierResult,
    )

    def fake_pre_refine(**kwargs):
        return PreRefineClassifierResult(
            triage_decision="REFINE",
            triage_reason="test",
            reviewer_agreement="AGREE",
            reviewer_agreement_reason="The draft looks good.",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.pre_refine_classifier.run_pre_refine_classifier",
        fake_pre_refine,
    )

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

    from robotsix_mill.stages.base import Outcome
    from robotsix_mill.stages.refine.core import RefineStage

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

    from robotsix_mill.core.constants import DEDUP_DUPLICATE_PREFIX
    from robotsix_mill.stages.base import Outcome
    from robotsix_mill.stages.refine.core import RefineStage

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


def test_language_gating_flutter_repo_includes_markers(tmp_path):
    """A Flutter-declaring repo MUST include Flutter/Dart-convention markers
    — ensures the flutter.md snippet is loaded and gated correctly."""
    import yaml as _yaml

    repo_dir = tmp_path / "flutter_repo"
    repo_dir.mkdir()
    config_dir = repo_dir / ".robotsix-mill"
    config_dir.mkdir()
    config = {"languages": ["flutter"]}
    (config_dir / "config.yaml").write_text(_yaml.dump(config), encoding="utf-8")

    from robotsix_mill.config import Settings as S
    from robotsix_mill.config.repo_settings import resolve_language_instructions

    settings = S(data_dir=str(tmp_path))
    lang_block = resolve_language_instructions(settings, repo_dir)

    assert lang_block, "Expected non-empty language block for Flutter repo"
    # Key Flutter markers should be present.
    assert "flutter analyze" in lang_block, (
        "Flutter repo must include 'flutter analyze' marker"
    )
    assert "pubspec.yaml" in lang_block, (
        "Flutter repo must include 'pubspec.yaml' marker"
    )
    assert "Dart SDK" in lang_block, "Flutter repo must include 'Dart SDK' marker"
