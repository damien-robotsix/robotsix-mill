"""Tests for :mod:`robotsix_mill.agents.copy_pasting`.

The copy-paste wrapper is a small layer over ``run_periodic_agent``
that bakes the copy-paste-specific knobs (``MAX_GAPS``,
``include_jscpd=True``, the agent's prompt tail, and the
``copy_paste_model`` fallback). The deeper periodic-base pipeline is
covered by ``tests/agents/test_periodic_base.py``; this file pins the
wrapper's contract: which kwargs it forwards, what its hardcoded
values are, the ``CopyPasteResult`` model defaults, and that the
shipped YAML's TRIAGE RULES survive in ``SYSTEM_PROMPT``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_mill.agents import copy_pasting
from robotsix_mill.agents.copy_pasting import (
    MAX_GAPS,
    CopyPasteResult,
    run_copy_paste_agent,
)
from robotsix_mill.config import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=str(tmp_path), copy_paste_model="copy-paste-model-id")


@pytest.fixture
def fake_periodic(monkeypatch):
    """Replace ``run_periodic_agent`` with a capture-and-return stub.

    The wrapper imports ``run_periodic_agent`` lazily inside the
    function body, so we patch the source module's attribute — the
    import resolves to that module at call time and picks up our
    stub.
    """
    captured: dict = {}

    def stub(**kwargs):
        captured["kwargs"] = kwargs
        return CopyPasteResult(
            updated_memory="updated",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    from robotsix_mill.agents import periodic_base

    monkeypatch.setattr(periodic_base, "run_periodic_agent", stub)
    return captured


# ---------------------------------------------------------------------------
# Parameter plumbing — every wrapper argument reaches run_periodic_agent
# ---------------------------------------------------------------------------


def test_run_copy_paste_agent_forwards_settings(settings, fake_periodic):
    """The caller's ``settings`` instance is forwarded unmodified."""
    run_copy_paste_agent(settings=settings)
    assert fake_periodic["kwargs"]["settings"] is settings


def test_run_copy_paste_agent_forwards_memory_recent_verified_repo_dir(
    tmp_path, settings, fake_periodic
):
    """``memory``, ``recent_proposals``, ``verified_proposals`` and
    ``repo_dir`` flow through unchanged."""
    repo_dir = tmp_path / "repo"
    run_copy_paste_agent(
        settings=settings,
        memory="mem",
        recent_proposals="recent",
        verified_proposals="verified",
        repo_dir=repo_dir,
    )
    kw = fake_periodic["kwargs"]
    assert kw["memory"] == "mem"
    assert kw["recent_proposals"] == "recent"
    assert kw["verified_proposals"] == "verified"
    assert kw["repo_dir"] == repo_dir


def test_run_copy_paste_agent_default_memory_strings_are_empty(settings, fake_periodic):
    """When the caller omits memory/recent/verified, the wrapper
    forwards empty strings (the agent starts a fresh ledger)."""
    run_copy_paste_agent(settings=settings)
    kw = fake_periodic["kwargs"]
    assert kw["memory"] == ""
    assert kw["recent_proposals"] == ""
    assert kw["verified_proposals"] == ""
    # repo_dir defaults to None when the caller has no repo clone.
    assert kw["repo_dir"] is None


# ---------------------------------------------------------------------------
# Hardcoded constants — the wrapper's purpose is to bake these in
# ---------------------------------------------------------------------------


def test_max_gaps_constant_is_eight():
    """``MAX_GAPS`` is the public clip cap used by the periodic runner
    via ``run_periodic_agent(..., max_gaps=MAX_GAPS)`` — pin the value
    so a silent edit doesn't double the volume of copy-paste tickets
    filed per pass."""
    assert MAX_GAPS == 8


def test_run_copy_paste_agent_forwards_max_gaps_and_include_jscpd(
    settings, fake_periodic
):
    """``max_gaps`` and ``include_jscpd=True`` are baked in by the
    wrapper — they are not exposed as caller kwargs."""
    run_copy_paste_agent(settings=settings)
    kw = fake_periodic["kwargs"]
    assert kw["max_gaps"] == MAX_GAPS
    assert kw["include_jscpd"] is True


def test_run_copy_paste_agent_uses_definition_name_copy_paste(settings, fake_periodic):
    """The default path resolves the built-in YAML by name —
    ``definition_name="copy_paste"`` selects
    ``agent_definitions/periodic/copy_paste.yaml``."""
    run_copy_paste_agent(settings=settings)
    assert fake_periodic["kwargs"]["definition_name"] == "copy_paste"


def test_run_copy_paste_agent_prompt_tail(settings, fake_periodic):
    """The wrapper bakes the prompt-tail string that nudges the agent
    to call ``detect_duplication`` once and triage. Pin it so silent
    rewordings (which would change the agent's behaviour) surface
    here."""
    run_copy_paste_agent(settings=settings)
    assert fake_periodic["kwargs"]["prompt_tail"] == (
        "Run detect_duplication, triage the clone pairs, and return your findings."
    )


def test_run_copy_paste_agent_uses_copy_paste_model_setting(settings, fake_periodic):
    """The model fallback is ``settings.copy_paste_model`` — when the
    YAML's ``model:`` is None this is the model name that wins."""
    run_copy_paste_agent(settings=settings)
    assert fake_periodic["kwargs"]["model_setting"] == settings.copy_paste_model
    # And the sentinel from the fixture really did flow through.
    assert fake_periodic["kwargs"]["model_setting"] == "copy-paste-model-id"


# ---------------------------------------------------------------------------
# definition_override branching
# ---------------------------------------------------------------------------


def test_definition_override_none_is_forwarded(settings, fake_periodic):
    """The default ``definition_override=None`` reaches the runner so
    its built-in-YAML path activates (the wrapper does NOT swallow
    the override kwarg)."""
    run_copy_paste_agent(settings=settings)
    assert "definition_override" in fake_periodic["kwargs"]
    assert fake_periodic["kwargs"]["definition_override"] is None


def test_definition_override_value_is_forwarded(settings, fake_periodic):
    """When the supervisor supplies a per-repo merged definition, the
    wrapper passes the override through verbatim — the bypass
    semantics are owned by ``run_periodic_agent`` (covered in
    ``test_periodic_base.test_definition_override_bypasses_builtin_*``)."""
    sentinel = object()
    run_copy_paste_agent(settings=settings, definition_override=sentinel)
    assert fake_periodic["kwargs"]["definition_override"] is sentinel


# ---------------------------------------------------------------------------
# CopyPasteResult model — pydantic defaults
# ---------------------------------------------------------------------------


def test_copy_paste_result_default_values():
    """A bare ``CopyPasteResult()`` has the documented defaults: empty
    string memory and three empty lists. Anything else would silently
    forward stale data to the periodic runner."""
    r = CopyPasteResult()
    assert r.updated_memory == ""
    assert r.draft_titles == []
    assert r.draft_bodies == []
    assert r.gap_ids == []


def test_copy_paste_result_list_defaults_are_per_instance():
    """The list defaults are constructed via ``Field(default_factory=list)``
    so two instances do not share the same backing list."""
    a = CopyPasteResult()
    b = CopyPasteResult()
    a.draft_titles.append("x")
    assert b.draft_titles == []


def test_copy_paste_result_round_trip():
    """All fields round-trip — the model is what the agent returns
    and what the runner consumes."""
    r = CopyPasteResult(
        updated_memory="m",
        draft_titles=["t"],
        draft_bodies=["b"],
        gap_ids=["g"],
    )
    assert r.updated_memory == "m"
    assert r.draft_titles == ["t"]
    assert r.draft_bodies == ["b"]
    assert r.gap_ids == ["g"]


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT canary — the shipped YAML's load-bearing content
# ---------------------------------------------------------------------------


def test_system_prompt_loaded_from_shipped_yaml():
    """``SYSTEM_PROMPT`` is loaded from
    ``agent_definitions/periodic/copy_paste.yaml`` at module-import
    time and is non-empty."""
    assert isinstance(copy_pasting.SYSTEM_PROMPT, str)
    assert len(copy_pasting.SYSTEM_PROMPT) > 200
    # The canary points back at the shipped YAML.
    yaml_path = (
        Path(copy_pasting.__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "periodic"
        / "copy_paste.yaml"
    )
    assert yaml_path.is_file(), f"shipped YAML missing at {yaml_path}"


def test_system_prompt_contains_triage_rules_canary():
    """The TRIAGE RULES section is the load-bearing piece — if it
    silently disappears, the agent files high-noise clone tickets.
    Pin the canary substrings so a rewording surfaces here."""
    p = copy_pasting.SYSTEM_PROMPT
    assert "TRIAGE RULES" in p
    # The two structural rules every clone ticket depends on.
    assert "≥3 files" in p
    assert "≥30 duplicated lines" in p
