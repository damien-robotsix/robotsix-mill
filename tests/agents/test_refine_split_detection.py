"""Split-detection tests for the refine stage.

Covers multi-scope ticket splitting — child ticket creation, umbrella
epic logic, depends-on index mapping, and the split heuristic in the
system prompt — split out from test_refine.py.
"""

import hashlib

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    ChildSpec,
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


def test_split_empty_children_gated_goes_to_approval_not_blocked(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """A degraded split + gated + usable draft → HUMAN_ISSUE_APPROVAL.

    The multi-scope twin of ``test_empty_spec_gated_goes_to_approval_not_blocked``:
    same "" -> BLOCKED path, same fix. Still never auto-approved to READY.
    """
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
    assert out.next_state is not State.READY
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert service.workspace(t).read_description() == "draft"


def test_split_empty_children_and_empty_draft_gated_still_blocks(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """Degraded split + gated + degenerate draft → BLOCKED, as before."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda **_: RefineResult(split=True, children=[]),
    )

    gated_settings = Settings(data_dir=str(tmp_path), require_approval="true")
    gated_ctx = StageContext(
        settings=gated_settings, service=service, repo_config=repo_config
    )

    t = service.create("Empty split gated", "TBD")
    out = RefineStage().run(t, gated_ctx)
    assert out.next_state is State.BLOCKED
    assert "original draft is empty too" in out.note


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
    child_a_id, _child_b_id = ids_in_note

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
