"""Config pin-drift runner — reports pinned settings that shadow a changed
code default.

A deterministic, no-LLM pass: read the config file, compare each pinned value
against the model's default, report what diverged.  No AI agent, no
pass_runner, no Langfuse tracing.

**Why this exists.** ``config/config.json`` pins ~288 settings explicitly, and a
pin always wins over the model default.  So changing a ``Field(default=...)`` is
a **no-op in production** unless somebody also edits the pin.  Nothing surfaced
that, and it bit the fleet repeatedly:

* 2026-08-07 — twelve periodic generators (``audit``, ``survey``, ``health``,
  ``meta``, ``test_gap`` and eight more) had been moved to a weekly cadence in
  code, but ran **daily** in production for weeks because the pins still said
  86400.  Ticket creation ran roughly 7x higher than intended: ~150/day against
  ~30/day before the drift, and ``audit`` + ``survey`` alone accounted for 471
  of 1368 tickets ever filed.
* The same day — per-ticket spend and trace caps read their old pinned values
  after the code defaults were changed to disable them.

Both were found by hand, long after the fact.  This pass makes the class
visible within one interval.

**Signal, not noise.** Most of the 288 pins are deliberate: an operator chose a
value that is not the default and should stay.  Reporting all of them every pass
would be unreadable, so drift is compared against a baseline of
already-reviewed keys (``config_pin_drift_baseline``), in the same
ratchet style as the repo's mypy baseline.  Only *new* divergence is reported —
which is exactly the case where a code default moved underneath a pin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...config import Settings

log = logging.getLogger("robotsix_mill.config_pin_drift")


@dataclass
class PinDrift:
    """One pinned setting whose value differs from the model default."""

    key: str
    pinned: Any
    default: Any

    def describe(self) -> str:
        """One-line, human-readable summary."""
        return f"{self.key}: pinned={self.pinned!r} default={self.default!r}"


@dataclass
class PinDriftResult:
    """Outcome of a single pass."""

    checked: int = 0
    drifted: list[PinDrift] = field(default_factory=list)
    baselined: int = 0


def _settings_pins() -> dict[str, Any]:
    """Return the ``settings`` block of the operator's config file.

    Empty when no config file is configured or it cannot be parsed — a
    missing config is not drift, and this pass must never be the thing that
    breaks a deployment without one.
    """
    from ...config.loader import load_settings_block

    try:
        return load_settings_block() or {}
    except Exception:
        log.warning(
            "config-pin-drift: could not read the settings block", exc_info=True
        )
        return {}


def detect_pin_drift(settings: Settings) -> PinDriftResult:
    """Compare every pinned setting against its model default.

    Args:
        settings: the live settings object, used for the baseline list.

    Returns:
        A :class:`PinDriftResult`. ``drifted`` excludes keys named in
        ``settings.config_pin_drift_baseline``.
    """
    from ...config import Settings as SettingsModel

    pins = _settings_pins()
    fields = SettingsModel.model_fields
    baseline = set(getattr(settings, "config_pin_drift_baseline", ()) or ())

    result = PinDriftResult(baselined=len(baseline))
    for key, pinned in sorted(pins.items()):
        field_info = fields.get(key)
        if field_info is None:
            # A pin with no matching field is config-surface drift, which
            # scripts/check_config_sync.py already owns. Not this pass's job.
            continue
        result.checked += 1
        default = field_info.default
        # ``default_factory`` fields report PydanticUndefined here; comparing
        # against that would flag every list-valued setting on every pass.
        if default is None or repr(type(default)).endswith("PydanticUndefinedType'>"):
            continue
        if pinned == default:
            continue
        if key in baseline:
            continue
        result.drifted.append(PinDrift(key=key, pinned=pinned, default=default))
    return result


def run_config_pin_drift(settings: Settings) -> dict[str, Any]:
    """Run one pass and log any new drift.

    Returns a summary dict for the caller to log. Reporting is deliberately
    log-only: this pass exists because the board is already over-full, and a
    generator that files a ticket per drifting key would make the problem it
    was built to surface worse.
    """
    result = detect_pin_drift(settings)
    if result.drifted:
        log.warning(
            "config-pin-drift: %d pinned setting(s) shadow a changed code "
            "default — a Field(default=...) change is a NO-OP in production "
            "while the pin stands. Either update the pin in config.json or add "
            "the key to config_pin_drift_baseline to record it as deliberate:\n  %s",
            len(result.drifted),
            "\n  ".join(d.describe() for d in result.drifted),
        )
    else:
        log.info(
            "config-pin-drift: no new drift (%d pin(s) checked, %d baselined)",
            result.checked,
            result.baselined,
        )
    return {
        "checked": result.checked,
        "drifted": len(result.drifted),
        "baselined": result.baselined,
        "keys": [d.key for d in result.drifted],
    }
