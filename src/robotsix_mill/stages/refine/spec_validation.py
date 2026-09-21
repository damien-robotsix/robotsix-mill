"""Pure spec-quality validation helpers for the refine stage.

These functions check a draft/spec for completeness, emptiness, and
scope readiness.  They are pure (no I/O, no ``self``) and independent of
the approval/state-resolution logic in :mod:`.helpers`, which re-exports
them for backward compatibility (consumers import from ``.helpers``).
"""

from __future__ import annotations

import re

from ...core.text_noop import is_degenerate_body


def _spec_is_degenerate(spec: str | None) -> bool:
    """True when *spec* is empty or a placeholder pointer, not a real spec.

    The refine agent's structured ``spec_markdown`` occasionally collapses
    to a short reference like ``"(see spec above)"`` — non-empty, so the
    bare ``not spec.strip()`` guard misses it, and refine writes the
    pointer straight into the canonical ``description.md`` (blanking the
    ticket body on the board). Treat such degenerate output as "no spec"
    so refine falls back to the original draft instead of clobbering it.

    Thin wrapper over :func:`core.text_noop.is_degenerate_body`, which is
    shared with the ticket-spawning agents so a body they would file and a
    spec refine would reject can never disagree.
    """
    return is_degenerate_body(spec)


def _draft_is_near_empty(draft: str, min_chars: int = 50) -> bool:
    """True when *draft* is empty or trivially short after stripping.

    Strips markdown headings (``## …``, ``# …``), code fences and their
    content, and whitespace.  If the remaining text is under *min_chars*,
    the draft is considered near-empty and the fast-path must not fire.

    The default threshold of 50 characters catches completely empty
    descriptions (the 968f scenario) while allowing short-but-legitimate
    mechanical drafts (e.g. "Rename X to Y in file Z") to still fast-path.
    """
    if not draft or not draft.strip():
        return True
    # Strip markdown heading lines (## ..., # ...)
    stripped = re.sub(r"^\s*#{1,6}\s+.*$", "", draft, flags=re.MULTILINE)
    # Strip fenced code blocks and their content
    stripped = re.sub(r"```[^\n]*\n.*?```", "", stripped, flags=re.DOTALL)
    stripped = stripped.strip()
    return len(stripped) < min_chars


def _fast_path_scope_checks(draft: str) -> dict[str, str | bool]:
    """Run pre-auto-approve scope checks on *draft*.

    Returns a dict mapping check names to string results (``"pass"`` /
    ``"fail: <reason>"``) suitable for a triage note.

    Checks:

    - **empty/near-empty**: passes when the draft has ≥ 50 chars after
      stripping headings and code fences.
    - **file count**: passes when the draft mentions ≤ 7 distinct
      backtick-enclosed file paths (a heuristic for single-implement-run
      scope).  More than 7 signals a multi-file change that likely
      exceeds one PR.
    """
    checks: dict[str, str | bool] = {}
    # --- emptiness ---
    if _draft_is_near_empty(draft):
        checks["empty"] = "fail: draft is empty or < 50 chars after stripping"
    else:
        checks["empty"] = "pass"

    # --- file-count scope ---
    path_count = _count_distinct_backtick_paths(draft)
    if path_count > 7:
        checks["scope"] = (
            f"fail: {path_count} distinct file paths — "
            "likely exceeds single-implement-run scope"
        )
    else:
        checks["scope"] = f"pass ({path_count} files)"
    return checks


_PATH_RE = re.compile(r"`([^`]*/[^`]*\.[a-zA-Z]{1,10})`")


def _count_distinct_backtick_paths(text: str) -> int:
    """Return the number of distinct backtick-enclosed file paths in *text*."""
    return len(set(_PATH_RE.findall(text)))


def _draft_has_complete_spec(text: str) -> bool:
    """True when *text* is already a self-contained spec.

    Heuristic: the draft contains a markdown ``## Problem`` heading AND
    at least one of ``## Scope`` / ``## Acceptance criteria`` (case-
    insensitive, matched only on markdown heading lines). This guards the
    CI fast-path: a CI ticket whose draft is a raw error dump with no
    scope section still routes to the full refine agent.
    """
    if not text or not text.strip():
        return False

    # Match headings only at the start of a line (allow leading whitespace)
    # with 1-6 `#` characters.
    def _has_heading(title: str) -> bool:
        return bool(
            re.search(
                r"^\s*#{1,6}\s+" + re.escape(title) + r"\b",
                text,
                re.IGNORECASE | re.MULTILINE,
            )
        )

    if not _has_heading("Problem"):
        return False
    return any(_has_heading(h) for h in ("Scope", "Acceptance criteria", "Acceptance"))
