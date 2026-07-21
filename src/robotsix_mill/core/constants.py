"""Shared constant definitions used across the robotsix_mill codebase.

These are definitions that would otherwise be duplicated in multiple
modules (e.g. binary-file extension sets).  They live in ``core/``
because ``core/`` already hosts cross-cutting concerns like models,
states, and text utilities.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Non-implementation close prefixes — note-prefix constants marking a
# **non-implementation** closure (dedup, freshness, obsolescence, mill
# misroute).  The refine stage writes these prefixes on DRAFT→DONE
# transitions and reads them back in ``_is_valid_dedup_target`` to
# reject a candidate that was itself dedup-/freshness-closed.  They
# also gate the transition guard in ``_lifecycle.py`` which exempts
# legitimate non-implementation DONE closures from the branch/PR
# requirement for implementation tickets.
# ---------------------------------------------------------------------------
DEDUP_DUPLICATE_PREFIX: str = "duplicate of "
DEDUP_ALREADY_DONE_PREFIX: str = "already implemented in "
FRESHNESS_STALE_PREFIX: str = "stale or invalid finding"
OBSOLESCENCE_GAP_PREFIX: str = "obsolete — gap already resolved"
WORKFLOW_PORTABILITY_GATE_PREFIX: str = "internal workflow gate:"
NON_IMPLEMENTATION_CLOSE_PREFIXES: tuple[str, ...] = (
    DEDUP_DUPLICATE_PREFIX,
    DEDUP_ALREADY_DONE_PREFIX,
    FRESHNESS_STALE_PREFIX,
    OBSOLESCENCE_GAP_PREFIX,
    WORKFLOW_PORTABILITY_GATE_PREFIX,
)

# File extensions that are likely binary — should be skipped during
# text preview / log traversal.
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".gz",
        ".zip",
        ".tar",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pkl",
        ".pickle",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".ico",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".exe",
        ".bin",
        ".dat",
        ".elf",
    }
)
