"""Tests for the post-refinement spec review pass."""

import pytest

from robotsix_mill.agents.refining import (
    ChildSpec,
    RefineResult,
    SpecReviewResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage


VERBOSE_SPEC = """## Problem

The frobnicate function has no docstring, making it hard for new
contributors to understand its purpose.

Looking at the codebase, I found src/example/frob.py:42 where the
function is defined. I checked several call sites and determined that
the function signature is:

```python
def frobnicate(x: int, mode: str = "fast") -> bool:
```

I ran `rg frobnicate` and found 12 call sites across 5 files.

## Scope

Add a docstring to `frobnicate` in `src/example/frob.py` following
PEP 257 conventions.

## Acceptance criteria

- The docstring describes the parameters `x` and `mode`
- The docstring describes the return value
- The test `test_frobnicate_docstring` passes

## Out of scope / constraints

- Not changing function behaviour
- Not adding type annotations
"""

CONCISE_SPEC = """## Problem

The frobnicate function has no docstring, making it hard for new
contributors to understand its purpose.

## Scope

Add a docstring to `frobnicate` in `src/example/frob.py` following
PEP 257 conventions.

## Acceptance criteria

- The docstring describes the parameters `x` and `mode`
- The docstring describes the return value
- The test `test_frobnicate_docstring` passes

## Out of scope / constraints

- Not changing function behaviour
- Not adding type annotations
"""


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


@pytest.fixture
def review_settings(tmp_path) -> Settings:
    """Settings with spec review enabled, triage disabled, no approval
    required — simplifies the stage flow so we only test the review wiring."""
    from robotsix_mill.core import db as dbmod

    dbmod.reset_engine()
    s = Settings(
        data_dir=str(tmp_path),
        require_approval="false",
        refine_triage_enabled="false",
        spec_review_enabled="true",
    )
    dbmod.init_db(s, board_id="test-board")
    yield s
    dbmod.reset_engine()


@pytest.fixture
def review_service(review_settings):
    from robotsix_mill.core.service import TicketService

    return TicketService(review_settings, board_id="test-board")


@pytest.fixture
def review_ctx(review_settings, review_service, repo_config):
    return StageContext(
        settings=review_settings, service=review_service, repo_config=repo_config
    )


# -----------------------------------------------------------------------
# Unit: single-spec path with mocked review_spec_for_conciseness
# -----------------------------------------------------------------------


def test_single_spec_review_saves_verbose_and_writes_concise(
    review_ctx,
    review_service,
    monkeypatch,
):
    """Mock review_spec_for_conciseness; verify verbose original saved as
    refine-verbose.md and concise version written as description.md."""
    # Mock dedup check — no duplicates.
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.dedup.run_dedup_check",
        lambda **kw: {
            "duplicate_of": None,
            "already_done": None,
            "reason": "unique",
        },
    )
    # Mock run_refine_agent — returns verbose spec, no split.
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.refining.run_refine_agent",
        lambda **kw: RefineResult(spec_markdown=VERBOSE_SPEC, split=False),
    )
    # Mock review_spec_for_conciseness — returns concise spec.
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.refining.review_spec_for_conciseness",
        lambda **kw: SpecReviewResult(
            concise_spec=CONCISE_SPEC,
            stripped_summary="Stripped 8 lines of exploratory narrative",
        ),
    )

    ticket = review_service.create("Add docstring to frobnicate", VERBOSE_SPEC)
    stage = RefineStage()
    outcome = stage.run(ticket, review_ctx)

    # Should reach READY (no approval required).
    assert outcome.next_state == State.READY

    # The description should be the concise version.
    ws = review_service.workspace(ticket)
    written = ws.read_description()
    assert "## Problem" in written
    assert "## Scope" in written
    assert "## Acceptance criteria" in written
    assert "## Out of scope" in written
    assert "I found" not in written
    assert "I checked" not in written
    assert "Looking at" not in written
    assert "I ran" not in written
    assert written.strip() == CONCISE_SPEC.strip()

    # The verbose original should be saved as an artifact.
    verbose_path = ws.artifacts_dir / "refine-verbose.md"
    assert verbose_path.exists()
    saved_verbose = verbose_path.read_text(encoding="utf-8")
    assert saved_verbose.strip() == VERBOSE_SPEC.strip()


# -----------------------------------------------------------------------
# Flag-off regression
# -----------------------------------------------------------------------


def test_flag_off_no_review_and_no_artifact(
    ctx,
    service,
    monkeypatch,
    tmp_path,
):
    """With spec_review_enabled=False, no review is applied and
    no refine-verbose.md artifact is saved."""
    ctx.settings.spec_review_enabled = False
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.dedup.run_dedup_check",
        lambda **kw: {
            "duplicate_of": None,
            "already_done": None,
            "reason": "unique",
        },
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.refining.run_refine_agent",
        lambda **kw: RefineResult(spec_markdown=VERBOSE_SPEC, split=False),
    )

    ticket = service.create("Add docstring to frobnicate", VERBOSE_SPEC)
    stage = RefineStage()
    outcome = stage.run(ticket, ctx)

    assert outcome.next_state == State.READY

    ws = service.workspace(ticket)
    written = ws.read_description()

    # The verbose spec is written as-is (no review pass).
    assert "I found" in written
    assert "I checked" in written
    assert "Looking at" in written

    # No refine-verbose.md artifact.
    verbose_path = ws.artifacts_dir / "refine-verbose.md"
    assert not verbose_path.exists()


# -----------------------------------------------------------------------
# Sendback bypass
# -----------------------------------------------------------------------


def test_reviewer_comments_bypasses_review(
    review_ctx,
    review_service,
    monkeypatch,
):
    """When reviewer_comments are present, the review pass is NOT applied."""
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.dedup.run_dedup_check",
        lambda **kw: {
            "duplicate_of": None,
            "already_done": None,
            "reason": "unique",
        },
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.refining.run_refine_agent",
        lambda **kw: RefineResult(spec_markdown=VERBOSE_SPEC, split=False),
    )

    ticket = review_service.create("Add docstring to frobnicate", VERBOSE_SPEC)
    # Add a reviewer comment to trigger the sendback bypass.
    review_service.add_comment(ticket.id, "Please clarify the acceptance criteria.")

    stage = RefineStage()
    outcome = stage.run(ticket, review_ctx)

    assert outcome.next_state == State.READY

    ws = review_service.workspace(ticket)
    written = ws.read_description()

    # The verbose spec is preserved as-is — no review pass.
    assert "I found" in written
    assert "Looking at" in written

    # No refine-verbose.md artifact.
    verbose_path = ws.artifacts_dir / "refine-verbose.md"
    assert not verbose_path.exists()


# -----------------------------------------------------------------------
# Split path
# -----------------------------------------------------------------------


def test_split_path_reviews_each_child(
    review_ctx,
    review_service,
    monkeypatch,
):
    """When the refine agent splits into children, each child's spec is
    reviewed and its verbose original saved as refine-verbose-child-N.md."""
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.dedup.run_dedup_check",
        lambda **kw: {
            "duplicate_of": None,
            "already_done": None,
            "reason": "unique",
        },
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.refining.run_refine_agent",
        lambda **kw: RefineResult(
            split=True,
            children=[
                ChildSpec(title="Child 1", spec_markdown=VERBOSE_SPEC),
                ChildSpec(title="Child 2", spec_markdown=VERBOSE_SPEC),
            ],
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.refining.review_spec_for_conciseness",
        lambda *, spec_markdown, **kw: SpecReviewResult(
            concise_spec=CONCISE_SPEC,
            stripped_summary="Stripped 8 lines",
        ),
    )

    ticket = review_service.create("Add docstring to frobnicate", VERBOSE_SPEC)
    stage = RefineStage()
    outcome = stage.run(ticket, review_ctx)

    assert outcome.next_state == State.CLOSED
    assert "split into" in outcome.note

    # Verify child tickets were created with concise specs.
    child_ids = outcome.note.replace("split into ", "").split(", ")
    assert len(child_ids) == 2
    for cid in child_ids:
        child = review_service.get(cid)
        assert child is not None
        desc = review_service.workspace(child).read_description()
        assert "## Problem" in desc
        assert "I found" not in desc

    # Verify verbose artifacts saved in parent workspace.
    ws = review_service.workspace(ticket)
    for i in (1, 2):
        verbose_path = ws.artifacts_dir / f"refine-verbose-child-{i}.md"
        assert verbose_path.exists()
        assert verbose_path.read_text(encoding="utf-8").strip() == VERBOSE_SPEC.strip()
