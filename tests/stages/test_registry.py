"""Tests for ``stages.registry`` — the single name → Stage map.

The registry is critical glue: every stage looked up by the worker
goes through ``get_stage(name)``. If a state in ``STAGE_FOR_STATE``
loses its registry entry, the worker silently skips that ticket
forever; the unit test below catches that drift the moment it
happens (and avoids the multi-hour debug of "why is my ticket stuck
in REVIEW?").
"""

from __future__ import annotations

import pytest

from robotsix_mill.core.states import STAGE_FOR_STATE
from robotsix_mill.stages.base import Stage
from robotsix_mill.stages.registry import STAGES, get_stage


def test_registry_covers_every_stage_for_state():
    """Module docstring promises: STAGES must cover every value in
    STAGE_FOR_STATE. Without this, transitioning a ticket into a
    state whose stage isn't registered would silently skip the
    worker dispatch and leave the ticket stuck."""
    missing = sorted(set(STAGE_FOR_STATE.values()) - set(STAGES))
    assert not missing, (
        f"STAGE_FOR_STATE points at stages with no entry in "
        f"stages.registry.STAGES: {missing}. Either register the "
        f"stage in ``_REGISTERED`` or fix the typo in STAGE_FOR_STATE."
    )


def test_registry_keys_match_stage_class_names():
    """Each entry's key in STAGES must match the registered class's
    ``name`` attribute. (Subclass with a wrong ``name`` would shadow
    the previous entry silently.)"""
    for key, stage in STAGES.items():
        assert isinstance(stage, Stage)
        assert key == stage.name, (
            f"STAGES key {key!r} doesn't match stage.name "
            f"{stage.name!r} for class {type(stage).__name__}"
        )


def test_get_stage_returns_same_singleton_each_call():
    """The dict-level lookup means subsequent calls return the SAME
    instance, not a fresh one — stages are stateless but the test
    pins the cheap invariant anyway."""
    a = get_stage("refine")
    b = get_stage("refine")
    assert a is b


def test_get_stage_unknown_raises_key_error_with_helpful_message():
    """get_stage on an unknown name must surface the available stage
    names so a typo doesn't waste a debug round-trip."""
    with pytest.raises(KeyError) as excinfo:
        get_stage("definitely-not-a-stage")
    msg = str(excinfo.value)
    assert "definitely-not-a-stage" in msg
    # The message should list the available stages so a typo is
    # obvious from the exception alone.
    assert "known" in msg.lower()
    # Spot-check that a real stage name appears.
    assert "refine" in msg
