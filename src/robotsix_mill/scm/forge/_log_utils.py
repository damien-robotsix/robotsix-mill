"""Log-window helpers shared by forge adapters.

Each forge supplies its own failure-marker regex so that
``_capture_failure_window`` anchors on platform-specific markers
(GitHub Actions ``##[error]``, GitLab CI ``^ERROR:``, etc.) rather
than imposing a one-size-fits-all regex.
"""

from __future__ import annotations

import re

# Regex patterns for stripping CI runner preamble/setup boilerplate.
# These lines carry no diagnostic value for the ci-fix agent and
# collectively account for hundreds of tokens per job log.
_RUNNER_PREAMBLE_RES: list[re.Pattern[str]] = [
    re.compile(r"^Current runner version:.*$", re.MULTILINE),
    re.compile(
        r"^##\[group\]Operating System\n.*?\n##\[endgroup\]", re.MULTILINE | re.DOTALL
    ),
    re.compile(
        r"^##\[group\]Runner Image\n.*?\n##\[endgroup\]", re.MULTILINE | re.DOTALL
    ),
    re.compile(
        r"^##\[group\]Runner Image Provisioner\n.*?\n##\[endgroup\]",
        re.MULTILINE | re.DOTALL,
    ),
    re.compile(
        r"^##\[group\]GITHUB_TOKEN Permissions\n.*?\n##\[endgroup\]",
        re.MULTILINE | re.DOTALL,
    ),
    re.compile(r"^Secret source:.*$\n?", re.MULTILINE),
    re.compile(r"^Prepare workflow directory\n?", re.MULTILINE),
    re.compile(
        r"^Prepare all required actions\n.*?(?=\n##\[group\])", re.MULTILINE | re.DOTALL
    ),
    re.compile(
        r"^Getting action download info\n.*?(?=\n##\[group\])",
        re.MULTILINE | re.DOTALL,
    ),
    # Standalone "Download action repository '…'" lines — these appear
    # when the "Prepare all required actions" / "Getting action download
    # info" blocks end without a ##[group] marker (the lookahead above
    # misses them).
    re.compile(r"^Download action repository '.*$", re.MULTILINE),
    re.compile(r"^Post job cleanup\.\n?", re.MULTILINE),
    # Collapse consecutive blank lines (3+) into at most 1 blank line.
    re.compile(r"\n{3,}", re.MULTILINE),
]


def _strip_runner_noise(clean_log: str) -> str:
    """Remove CI runner boilerplate from a cleaned (ANSI-stripped) job log.

    Strips known GitHub Actions runner preamble blocks, then collapses
    consecutive blank lines.  Returns *clean_log* unchanged when no
    patterns match.  This is a pure token-saver — it never removes error
    lines, step output, or anything with diagnostic value.

    ``##[group]`` / ``##[endgroup]`` markers are left intact: they are a
    few bytes each and provide step-boundary context the ci-fix agent
    uses to identify which step failed.
    """
    for pat in _RUNNER_PREAMBLE_RES:
        clean_log = pat.sub("", clean_log)
    # Collapse resulting blank-line runs.
    clean_log = re.sub(r"\n{3,}", "\n\n", clean_log)
    return clean_log.strip()


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
