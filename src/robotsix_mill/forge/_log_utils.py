"""Log-window helpers shared by forge adapters.

Each forge supplies its own failure-marker regex so that
``_capture_failure_window`` anchors on platform-specific markers
(GitHub Actions ``##[error]``, GitLab CI ``^ERROR:``, etc.) rather
than imposing a one-size-fits-all regex.
"""

from __future__ import annotations

import re


def _capture_failure_window(
    clean_log: str,
    max_bytes: int,
    *,
    failure_re: re.Pattern[str],
    tail_context: int = 4096,
) -> str:
    """Return at most *max_bytes* of *clean_log*, centred on the FIRST
    *failure_re* marker so an ``if: always()`` cascade can't mask the
    real failing step.

    If the log fits, it's returned whole.  If no failure marker is
    found (or it already falls inside the tail window), this degrades
    to the historical tail-cap (keep the last *max_bytes*).
    """
    if len(clean_log) <= max_bytes:
        return clean_log
    m = failure_re.search(clean_log)
    if m is None or m.start() >= len(clean_log) - max_bytes:
        # No marker, or the first marker is already within the tail window →
        # the tail-cap already shows it.
        return clean_log[-max_bytes:]
    # Anchor: spend most of the budget on the lead-up to the first marker
    # (where the real error message lives), keeping a little after it. Cap the
    # after-context at half the budget so a marker near the log start still
    # keeps its preceding lines.
    tail_after = min(tail_context, max_bytes // 2)
    start = max(0, m.start() - (max_bytes - tail_after))
    end = min(len(clean_log), start + max_bytes)
    prefix = "[log truncated — window anchored on first failure marker]\n"
    return prefix + clean_log[start:end]
