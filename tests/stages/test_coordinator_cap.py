"""Tests for the per-ticket coordinator tool-call cap override.

Covers the effective-cap helper (default vs override vs hard-cap clamp),
the service setter that stores/clears the override, and its visibility on
the ``Ticket`` model and ``TicketRead`` API shape.
"""

from __future__ import annotations

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core.models import Ticket
from robotsix_mill.stages._coordinator_cap import (
    effective_coordinator_max_tool_calls,
)


def _settings(**over) -> Settings:
    return Settings(
        data_dir="/tmp", require_approval="false", disk_min_free_mb=0, **over
    )


def test_no_override_uses_default_cap():
    """A ticket without an override uses coordinator_max_tool_calls."""
    s = _settings()
    t = Ticket(id="t1", title="x", workspace_path="w")
    assert t.coordinator_max_tool_calls_override is None
    assert effective_coordinator_max_tool_calls(s, t) == 300


def test_override_below_hard_cap_is_used_verbatim():
    """An approved override under the hard cap raises the effective cap so a
    large ticket can exceed the 300 default in one pass."""
    s = _settings()  # hard cap default 1000
    t = Ticket(
        id="t2", title="x", workspace_path="w", coordinator_max_tool_calls_override=600
    )
    assert effective_coordinator_max_tool_calls(s, t) == 600


def test_override_is_clamped_to_hard_cap():
    """A sky-high override can never disable the runaway-loop backstop — the
    effective cap is min(override, hard_cap)."""
    s = _settings()  # hard cap default 1000
    t = Ticket(
        id="t3",
        title="x",
        workspace_path="w",
        coordinator_max_tool_calls_override=100_000,
    )
    assert effective_coordinator_max_tool_calls(s, t) == 1000


def test_override_respects_configured_hard_cap():
    """The hard cap is operator-tunable; the clamp uses the configured value."""
    s = _settings(coordinator_max_tool_calls_hard_cap=500)
    t = Ticket(
        id="t4", title="x", workspace_path="w", coordinator_max_tool_calls_override=900
    )
    assert effective_coordinator_max_tool_calls(s, t) == 500


def test_override_can_lower_the_cap():
    """An override below the default lowers the effective cap."""
    s = _settings()
    t = Ticket(
        id="t5", title="x", workspace_path="w", coordinator_max_tool_calls_override=50
    )
    assert effective_coordinator_max_tool_calls(s, t) == 50


def test_override_never_below_one():
    """A non-positive override floors at 1 rather than disabling the cap."""
    s = _settings()
    t = Ticket(
        id="t6", title="x", workspace_path="w", coordinator_max_tool_calls_override=0
    )
    assert effective_coordinator_max_tool_calls(s, t) == 1


def test_setter_stores_and_clears_override(service):
    """set_coordinator_max_tool_calls_override persists the value and clears
    it back to None."""
    t = service.create("large ticket")
    assert t.coordinator_max_tool_calls_override is None

    service.set_coordinator_max_tool_calls_override(t.id, 800)
    assert service.get(t.id).coordinator_max_tool_calls_override == 800

    service.set_coordinator_max_tool_calls_override(t.id, None)
    assert service.get(t.id).coordinator_max_tool_calls_override is None


def test_setter_rejects_non_positive(service):
    """A non-positive override is rejected; None is the way to clear."""
    t = service.create("large ticket")
    with pytest.raises(ValueError):
        service.set_coordinator_max_tool_calls_override(t.id, 0)


def test_override_visible_in_ticket_read(service, settings):
    """The override surfaces on the TicketRead API shape (ticket metadata)."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("large ticket")
    service.set_coordinator_max_tool_calls_override(t.id, 700)
    read = enrich_ticket_read(
        service.get(t.id),
        settings,
        service,
        blocking_cost=False,
        fetch_pr_url=False,
    )
    assert read.coordinator_max_tool_calls_override == 700
