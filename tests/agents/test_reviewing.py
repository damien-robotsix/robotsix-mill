"""Prompt-content tests for the reviewing agent.

Validates that the SYSTEM_PROMPT and field description contain the
semantic anchors required by the auto-merge eligibility design.
"""

import pytest

from robotsix_mill.agents.reviewing import SYSTEM_PROMPT, ReviewVerdict


# -- Field description ---------------------------------------------------

def test_auto_merge_eligible_description_approve_true():
    """Field description anchors: APPROVE + no specific concern → true."""
    desc = ReviewVerdict.model_fields["auto_merge_eligible"].description
    assert desc is not None
    desc_lower = desc.lower()
    assert "approve" in desc_lower
    assert "no specific concern" in desc_lower
    assert "true" in desc_lower


def test_auto_merge_eligible_description_named_reason_to_false():
    """Field description anchors: false requires a named, concrete reason."""
    desc = ReviewVerdict.model_fields["auto_merge_eligible"].description
    assert desc is not None
    desc_lower = desc.lower()
    assert "concrete reason" in desc_lower
    assert "name a concrete reason" in desc_lower
    assert "set to false" in desc_lower


def test_auto_merge_eligible_description_request_changes_false():
    """Field description: REQUEST_CHANGES / NEEDS_DISCUSSION → false."""
    desc = ReviewVerdict.model_fields["auto_merge_eligible"].description
    assert desc is not None
    desc_lower = desc.lower()
    assert "request_changes" in desc_lower
    assert "needs_discussion" in desc_lower
    assert "always leave this false" in desc_lower


# -- SYSTEM_PROMPT -------------------------------------------------------
# Normalise whitespace so assertions aren't tripped up by multi-line
# prose that wraps long lines at ~80 cols.

@pytest.fixture
def prompt() -> str:
    """SYSTEM_PROMPT lowercased with newlines collapsed to spaces."""
    return SYSTEM_PROMPT.lower().replace("\n", " ")


def test_system_prompt_approve_no_concern_true(prompt):
    """SYSTEM_PROMPT: APPROVE + no concern raised → true."""
    assert "approve" in prompt
    assert "raised no" in prompt
    assert "specific concern" in prompt
    assert "a human doesn't need to look" in prompt


def test_system_prompt_false_requires_articulable_reason(prompt):
    """SYSTEM_PROMPT: false only with articulable, specific reason."""
    assert "articulate a *specific* reason" in prompt
    assert "human should still look" in prompt
    assert "set this to ``false`` only when" in prompt


def test_system_prompt_request_changes_needs_discussion_false(prompt):
    """SYSTEM_PROMPT: REQUEST_CHANGES / NEEDS_DISCUSSION always false."""
    assert "request_changes" in prompt
    assert "needs_discussion" in prompt
    assert "always leave this" in prompt
    assert "``false``" in prompt


def test_system_prompt_tie_breaker_human_judgment_concern(prompt):
    """SYSTEM_PROMPT: tie-breaker re-aimed at human-judgment concern,
    not change-size."""
    assert "when unsure whether a genuine human-judgment concern" in prompt
    assert "default to ``false``" in prompt

    # The old size-based criteria must be GONE.
    assert "small and focused" not in prompt
    assert "single concern, few files" not in prompt
    assert "zero risk of regression" not in prompt
    assert "no new infrastructure" not in prompt


# -- Pydantic default unchanged ------------------------------------------

def test_auto_merge_eligible_default_is_false():
    """Pydantic default=False must be preserved — the prompt bias is what
    changes operational behaviour, not the structural fallback."""
    assert ReviewVerdict.model_fields["auto_merge_eligible"].default is False
