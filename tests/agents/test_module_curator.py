"""Tests for the module-curator agent and runner."""

import re

import pytest

from robotsix_mill.agents import module_curator as mc_agent
from robotsix_mill.module_curator_runner import run_module_curator_pass, ModuleCuratorPassResult
from robotsix_mill.pass_runner import _GAP_ID_RE


# --- Agent tests ---


def test_module_curator_system_prompt_covers_all_drift_classes():
    """The module-curator agent prompt must cover all three drift classes."""
    p = mc_agent.SYSTEM_PROMPT.lower()
    # 1. Unclassified files
    assert "unclassified" in p
    # 2. Stale paths
    assert "stale path" in p
    # 3. New module proposals
    assert "new module" in p
    # Must be read-only
    assert "read-only" in p or "read only" in p or "do not move" in p or "do not delete" in p
    # Must use the de-duplication guidance
    assert "de-duplication" in p or "deduplication" in p
    # Must reference docs/modules.yaml
    assert "docs/modules.yaml" in p or "modules.yaml" in p


def test_module_curator_result_model():
    """ModuleCuratorResult has the expected fields and defaults."""
    result = mc_agent.ModuleCuratorResult(
        updated_memory="memory",
        draft_titles=["title1"],
        draft_bodies=["body1"],
        gap_ids=["gap1"],
    )
    assert result.updated_memory == "memory"
    assert len(result.draft_titles) == 1
    assert len(result.draft_bodies) == 1
    assert len(result.gap_ids) == 1

    # Defaults
    default_result = mc_agent.ModuleCuratorResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_module_curator_result_field_types():
    """ModuleCuratorResult fields have correct types."""
    result = mc_agent.ModuleCuratorResult(
        updated_memory="# Module Curator Memory\n",
        draft_titles=["Classify file: assign to existing module or propose a new one"],
        draft_bodies=["The file src/foo.py is unclassified..."],
        gap_ids=["unclassified_foo"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)


def test_max_drafts_is_reasonable():
    """MAX_DRAFTS should be a positive integer."""
    assert isinstance(mc_agent.MAX_DRAFTS, int)
    assert mc_agent.MAX_DRAFTS > 0


def test_runner_stub_exists():
    """The runner stub should be callable with correct types."""
    assert callable(run_module_curator_pass)
    assert issubclass(ModuleCuratorPassResult, object)


def test_gap_id_re_matches_module_curator():
    """The _GAP_ID_RE must match module_curator markers so de-duplication works."""
    marker = "<!-- module_curator-gap-id: unclassified_src_foo -->"
    matches = _GAP_ID_RE.findall(marker)
    assert len(matches) == 1
    label, gap_id = matches[0]
    assert label == "module_curator"
    assert gap_id == "unclassified_src_foo"
