import json
from pathlib import Path

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    ChildSpec,
    FileMapEntry,
    RefineResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage
from tests.agents.conftest import (
    _single,
    _split,
)

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
    from robotsix_mill.agents.refining import REVIEWER_SENDBACK_PROMPT, SYSTEM_PROMPT

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

    # Override the autouse APPROVE passthrough: this test exercises the
    # gated path, so the auto-approve verdict must be NEEDS_APPROVAL.
    from robotsix_mill.agents import post_refine
    from robotsix_mill.agents.post_refine import PostRefineResult

    monkeypatch.setattr(
        post_refine,
        "run_post_refine_check",
        lambda *, settings, spec, reviewer_comments=None, **kw: PostRefineResult(
            concise_spec=spec,
            stripped_summary="test passthrough",
            auto_approve="NEEDS_APPROVAL",
            auto_approve_reason="design decision present",
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

    # Override the autouse APPROVE passthrough: this test exercises the
    # gated path, so the auto-approve verdict must be NEEDS_APPROVAL.
    from robotsix_mill.agents import post_refine
    from robotsix_mill.agents.post_refine import PostRefineResult

    monkeypatch.setattr(
        post_refine,
        "run_post_refine_check",
        lambda *, settings, spec, reviewer_comments=None, **kw: PostRefineResult(
            concise_spec=spec,
            stripped_summary="test passthrough",
            auto_approve="NEEDS_APPROVAL",
            auto_approve_reason="design decision present",
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

    from robotsix_mill.agents.yaml_loader import load_agent_definition

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent / "agent_definitions" / "refine.yaml"
    )
    assert definition.name == "refine"
    assert definition.output_type == "RefineResult"
    assert "explore" in definition.tools
    assert "read_file" in definition.tools
    assert "parallel_explore" in definition.tools
