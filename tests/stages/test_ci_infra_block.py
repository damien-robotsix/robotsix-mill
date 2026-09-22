"""Tests for the GitHub account/billing CI-block classifier.

Covers the 2026-09-18 outage signature: GitHub disabled hosted runners
for the account's PRIVATE repos, so every hosted job concluded ``failure``
with an EMPTY ``steps`` array and a single billing check-run annotation.
"""

from __future__ import annotations

from robotsix_mill.stages.ci_infra_block import (
    INFRA_ACCOUNT_BLOCK_MARKER,
    INFRA_ACCOUNT_BLOCKED,
    annotations_indicate_account_block,
    classify_ci_run,
    infra_account_block_note,
)

_BILLING = (
    "The job was not started because recent account payments have failed "
    "or your spending limit needs to be increased. Please check the "
    "'Billing & plans' section in your settings"
)


def test_empty_steps_plus_billing_annotation_is_account_blocked():
    """AC: jobs[].steps == [] + the billing annotation → INFRA_ACCOUNT_BLOCKED."""
    run = {
        "jobs": [
            {"name": "Quality gate", "steps": []},
            {"name": "Lint", "steps": []},
        ],
        "annotations": [{"message": _BILLING, "level": "failure"}],
    }
    assert classify_ci_run(run) == INFRA_ACCOUNT_BLOCKED


def test_billing_annotation_nested_under_failing_check():
    """The ci_fix gate shape: annotations live under ``failing`` checks."""
    run = {"failing": [{"name": "CI", "annotations": [{"message": _BILLING}]}]}
    assert classify_ci_run(run) == INFRA_ACCOUNT_BLOCKED


def test_billing_text_in_check_summary_is_detected():
    run = {"failing": [{"name": "CI", "summary": _BILLING, "annotations": []}]}
    assert classify_ci_run(run) == INFRA_ACCOUNT_BLOCKED


def test_plain_string_annotations_are_supported():
    run = {"jobs": [{"steps": []}], "annotations": [_BILLING]}
    assert annotations_indicate_account_block(run) is True
    assert classify_ci_run(run) == INFRA_ACCOUNT_BLOCKED


def test_real_failure_with_steps_is_not_account_blocked():
    """A genuine test failure has non-empty steps → never account-blocked."""
    run = {
        "jobs": [
            {"name": "tests", "steps": [{"name": "pytest", "conclusion": "failure"}]}
        ],
        "annotations": [{"message": "assert 1 == 2"}],
    }
    assert classify_ci_run(run) is None


def test_steps_present_rejects_even_coincidental_billing_text():
    """When step data exists it must corroborate — a job that actually ran
    (non-empty steps) is not the account block even if its logs quote the
    billing text."""
    run = {
        "jobs": [{"name": "x", "steps": [{"name": "run"}]}],
        "annotations": [{"message": _BILLING}],
    }
    assert classify_ci_run(run) is None


def test_empty_steps_without_billing_annotation_is_not_blocked():
    run = {
        "jobs": [{"steps": []}],
        "annotations": [{"message": "The operation was canceled"}],
    }
    assert classify_ci_run(run) is None


def test_no_annotations_returns_none():
    assert classify_ci_run({"jobs": [{"steps": []}]}) is None
    assert classify_ci_run({}) is None


def test_block_note_carries_marker():
    note = infra_account_block_note()
    assert note.startswith(INFRA_ACCOUNT_BLOCK_MARKER)
    assert "hosted" in note.lower()
