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


# ---------------------------------------------------------------------------
# consolidated-docs / sync-check class detection
# ---------------------------------------------------------------------------
#
# A draft proposing "consolidate duplicated <X>" or "add a check keeping
# <X> in sync across files" (docs-sync remediations) is only as sound as
# its enumeration of the duplicated copies.  Live chain
# (robotsix-linkedin, 2026-11-19): a finding enumerated 'five places'
# for a duplicated config table, the sync check it spawned hardcoded
# exactly those five, and a sixth copy (chat-skill.md) drifted
# undetected.  These detectors gate the refine mechanical fast-path so a
# hardcoded enumeration cannot auto-approve without a content-marker
# discovery sweep, and the refine agent's prompt rule
# (agent_definitions/refine.yaml) performs that sweep.

# Class markers: a draft that consolidates duplicated content or keeps
# content in sync across files.  Small, extensible list — no NLP
# generalisation (same convention as ``_OPS_SHAPE_PATTERNS`` in
# stages/refine/helpers.py).
_SYNC_CHECK_CLASS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bconsolidat\w*\s+(?:the\s+)?duplicat\w*", re.IGNORECASE),
    re.compile(
        r"\bduplicat\w*\s+(?:the\s+)?"
        r"(?:cop(?:y|ies)|config|doc|content|table|settings)",
        re.IGNORECASE,
    ),
    re.compile(r"\bcop(?:y|ies)\s+of\s+(?:the\s+)?(?:same|this|that)", re.IGNORECASE),
    re.compile(r"\bkeep\w*\s+[^\n]{0,80}\bin sync\b", re.IGNORECASE),
    re.compile(r"\bin sync\s+across\b", re.IGNORECASE),
    re.compile(r"\bsync[- ]check\b", re.IGNORECASE),
    re.compile(r"\bout\s+of\s+sync\b", re.IGNORECASE),
    re.compile(r"\b(?:doc|content|config|settings?)\s+drift\b", re.IGNORECASE),
)

# Evidence that the content-marker discovery step actually ran: a tree
# grep over a stable anchor, or explicit discovery narration.  A bare
# enumerated "verified set" / hardcoded file list is deliberately NOT
# evidence — that is precisely the incomplete-enumeration failure mode
# this gate exists to route around.
_DISCOVERY_EVIDENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgit grep\b", re.IGNORECASE),
    re.compile(r"\bcontent-marker discovery\b", re.IGNORECASE),
    re.compile(r"\bgrep-discovered\b", re.IGNORECASE),
    re.compile(r"\bevery file containing\b", re.IGNORECASE),
    re.compile(r"\bsibling cop(?:y|ies)\b", re.IGNORECASE),
)


def _sync_check_class_marker(text: str) -> str | None:
    """Return the matched substring of the first consolidated-docs /
    sync-check class pattern in *text*, or ``None``.

    Pure function — used by the refine fast-path gate to decide whether
    a draft proposes consolidating duplicated content or keeping content
    in sync across files.
    """
    for pattern in _SYNC_CHECK_CLASS_PATTERNS:
        m = pattern.search(text or "")
        if m is not None:
            return m.group(0)
    return None


def _spec_evidences_discovery(text: str) -> bool:
    """True when *text* carries evidence that a content-marker discovery
    sweep ran (a tree grep over an anchor, or explicit discovery
    narration).  A hardcoded "verified set" list with no discovery
    evidence returns False.
    """
    return any(p.search(text or "") for p in _DISCOVERY_EVIDENCE_PATTERNS)


def _sync_check_gate_blocks(text: str) -> str | None:
    """Return the matched class marker when the refine fast-path must
    NOT auto-approve *text*, else ``None``.

    A consolidated-docs / sync-check draft is auto-approvable only when
    its verified set equals the grep-discovered set of sibling copies.
    A hardcoded enumeration with no discovery evidence is exactly the
    incomplete-set failure mode (the live robotsix-linkedin chain) — so
    it must fall through to the full refine agent, whose prompt rule
    performs the content-marker discovery step, instead of fast-path
    approving.
    """
    marker = _sync_check_class_marker(text)
    if marker is None:
        return None
    return None if _spec_evidences_discovery(text) else marker
