"""Tests for the refine spec-drift guard (``stages/refine/_drift.py``).

The live specimen (2026-09-07, ticket ``…-a144``) is mirrored exactly:
the draft asks to fix the auto-approve "triage failed" fallback, the
refine result proposes a docstring-coverage task lifted from the memory
ledger, and a new title to match.
"""

from __future__ import annotations

import pytest

from robotsix_mill.agents.refining import ChildSpec, RefineResult
from robotsix_mill.core.states import State
from robotsix_mill.stages.refine import _drift

_A144_TITLE = (
    "Auto-approve 'triage failed' fallback routes trivially-approvable "
    "docs-only tickets to human_issue_approval"
)
_A144_DRAFT = (
    "single repo: yes — all changes within robotsix-mill; external changes: none\n\n"
    "Symptom (observed 2026-09-07 board-gates-drain): the draft fast-path "
    "auto-approve classifier reached a clear APPROVE or SKIP verdict for five "
    "trivially-approvable single-file documentation tickets (the 'Add compliant "
    "SECURITY.md to repo root' rollout), yet the final auto-approve result on "
    "each was 'auto-approve: triage failed — falling back to human approval', "
    "routing them to human_issue_approval instead of auto-approving to ready.\n\n"
    "Acceptance criteria:\n- A single-file docs-only ticket that the classifier "
    "marks APPROVE/SKIP auto-approves to ready with no human_issue_approval stop.\n"
    "- A genuine triage-step failure is logged as a distinct error and retried."
)
_A144_DRIFTED_SPEC = (
    "## Problem\n\nThe `docstring_coverage` periodic gate flagged that module "
    "docstring coverage in `config/repo_settings.py` is below the repo's 100% "
    "target. Several functions in that module lack docstrings — notably "
    "`_guard_repo_settings_file` and its private helper siblings.\n\n"
    "## Scope\n\n- Enumerate the functions in `config/repo_settings.py` that "
    "are missing a docstring by running the same check the periodic gate "
    "invokes.\n- Add one docstring per flagged function following the "
    "robotsix-standards Python docstring convention.\n\n"
    "## Acceptance criteria\n\n- The checker reports 100% module docstring coverage."
)
_A144_DRIFTED_TITLE = (
    "Add missing docstrings to config/repo_settings.py to restore 100% "
    "docstring coverage"
)
_A144_FAITHFUL_SPEC = (
    "## Problem\n\nIn `stages/refine/helpers.py::_resolve_next_state` the "
    "auto-approve triage call is wrapped in a bare `except Exception` that "
    "maps every failure — including the level-1 model answering prose "
    "instead of JSON — to `auto-approve: triage failed — falling back to "
    "human approval`, so docs-only tickets the classifier already approved "
    "park in human_issue_approval.\n\n## Scope\n\n- Parse a leading "
    "`APPROVE`/`NEEDS_APPROVAL` verdict out of prose answers before falling "
    "back.\n- Log the genuine triage failure as a distinct error and retry "
    "once.\n\n## Acceptance criteria\n\n- A docs-only SECURITY.md ticket the "
    "classifier marks APPROVE reaches ready without a human gate."
)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_title_anchors_drop_short_and_generic_terms():
    anchors = _drift.title_anchors("Add feature: fix the flaky tests in CI")
    # "feature"/"tests" are generic, "add"/"fix"/"the"/"in"/"ci" too short.
    assert anchors == {"flaky"}


def test_anchor_overlap_uses_prefix_stems():
    # "approve" must match "approval"/"approving"; unrelated words must not.
    assert _drift.anchor_overlap({"approve"}, "awaiting human approval") == 1.0
    assert _drift.anchor_overlap({"approve"}, "docstring coverage") == 0.0
    assert _drift.anchor_overlap(set(), "anything") == 1.0


def test_detect_spec_drift_flags_the_live_specimen():
    result = RefineResult(spec_markdown=_A144_DRIFTED_SPEC, title=_A144_DRIFTED_TITLE)
    drifted, detail = _drift.detect_spec_drift(
        _A144_TITLE, _A144_DRAFT, result, min_overlap=0.2, min_anchors=3
    )
    assert drifted is True
    assert "anchor terms" in detail


def test_detect_spec_drift_accepts_a_faithful_refine():
    result = RefineResult(spec_markdown=_A144_FAITHFUL_SPEC)
    drifted, _ = _drift.detect_spec_drift(
        _A144_TITLE, _A144_DRAFT, result, min_overlap=0.2, min_anchors=3
    )
    assert drifted is False


def test_detect_spec_drift_never_judges_vague_titles_from_the_draft():
    # One title anchor only ("widget"): the draft must NOT be used as a
    # fallback — CI-failure drafts are refined into differently-worded
    # root-cause specs and would false-positive.
    draft = (
        "The widget loader swallows IO errors so a missing config file looks "
        "like an empty config. The widget loader should raise ConfigMissing."
    )
    off_topic = RefineResult(
        spec_markdown="## Problem\n\nRotate the deployment credentials and "
        "document the runbook for the on-call rotation."
    )
    drifted, detail = _drift.detect_spec_drift(
        "Fix widget", draft, off_topic, min_overlap=0.2, min_anchors=3
    )
    assert drifted is False
    assert "too few title anchor terms" in detail


def test_detect_spec_drift_never_judges_thin_inputs():
    result = RefineResult(spec_markdown="## Problem\n\nSomething unrelated.")
    drifted, detail = _drift.detect_spec_drift(
        "Fix it", "Please fix it soon.", result, min_overlap=0.2, min_anchors=3
    )
    assert drifted is False
    assert "too few title anchor terms" in detail


def test_refined_text_includes_children_and_epic_body():
    result = RefineResult(
        split=True,
        children=[ChildSpec(title="Child A", spec_markdown="alpha body")],
        epic_body="epic omega",
    )
    text = _drift.refined_text(result)
    assert "Child A" in text and "alpha body" in text and "epic omega" in text


# ---------------------------------------------------------------------------
# guard wiring (settings + outcome + artifact)
# ---------------------------------------------------------------------------


class _WS:
    def __init__(self, tmp_path):
        self.artifacts_dir = tmp_path / "artifacts"


class _Settings:
    refine_drift_guard_enabled = True
    refine_drift_guard_min_overlap = 0.2
    refine_drift_guard_min_anchors = 3


class _Ticket:
    id = "20260907T120541Z-auto-approve-triage-failed-fallback-rout-a144"


@pytest.fixture
def drifted_result():
    return RefineResult(spec_markdown=_A144_DRIFTED_SPEC, title=_A144_DRIFTED_TITLE)


def test_guard_parks_and_preserves_rejected_spec(tmp_path, drifted_result):
    ws = _WS(tmp_path)
    out = _drift.spec_drift_guard(
        _Ticket(), _A144_TITLE, _A144_DRAFT, ws, _Settings(), drifted_result
    )
    assert out is not None
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "refine drift guard" in out.note
    assert _A144_TITLE in out.note and _A144_DRIFTED_TITLE in out.note
    saved = (ws.artifacts_dir / "refine-drift-rejected.md").read_text(encoding="utf-8")
    assert "docstring_coverage" in saved


def test_guard_passes_faithful_result(tmp_path):
    out = _drift.spec_drift_guard(
        _Ticket(),
        _A144_TITLE,
        _A144_DRAFT,
        _WS(tmp_path),
        _Settings(),
        RefineResult(spec_markdown=_A144_FAITHFUL_SPEC),
    )
    assert out is None


def test_guard_skips_no_change_results(tmp_path):
    result = RefineResult(
        no_change_needed=True, no_change_rationale="already fixed in #3158"
    )
    assert (
        _drift.spec_drift_guard(
            _Ticket(), _A144_TITLE, _A144_DRAFT, _WS(tmp_path), _Settings(), result
        )
        is None
    )


def test_guard_skips_promote_to_epic_results(tmp_path):
    result = RefineResult(promote_to_epic=True, epic_body="## Strategic epic body")
    assert (
        _drift.spec_drift_guard(
            _Ticket(), _A144_TITLE, _A144_DRAFT, _WS(tmp_path), _Settings(), result
        )
        is None
    )


def test_guard_can_be_disabled(tmp_path, drifted_result):
    class Off(_Settings):
        refine_drift_guard_enabled = False

    assert (
        _drift.spec_drift_guard(
            _Ticket(), _A144_TITLE, _A144_DRAFT, _WS(tmp_path), Off(), drifted_result
        )
        is None
    )
