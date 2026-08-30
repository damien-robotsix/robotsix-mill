"""Conventional-commit subjects for mill-authored commits and PRs.

Every fleet repo generates its changelog and its version bump from
conventional commits via release-please (robotsix-standards
``release-please.md``).  A commit whose subject does not start with a
recognised type is silently ignored by release-please: it lands on
``main`` but never appears in ``CHANGELOG.md`` and never contributes to
a version bump.

Mill has historically written ``mill: <title> (<id>)`` for both the
branch commit and the PR title, so every mill-authored change was
invisible to the release pipeline.

The type is not guessed here.  Two sources, in order:

1. A ticket title that already opens with a conventional type (the
   refine agent is asked to write titles that way) is authoritative.
2. Otherwise the implement agent states the type in its summary on a
   ``Change-Type: <type>`` line; :func:`record_type` parks it as a
   workspace artifact so deliver can still classify the PR title later.

No fragment files, no extra LLM call.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Used when neither source names a type.  Deliberately a type that
# neither bumps the version nor appears in the changelog: inventing a
# ``feat``/``fix`` would fabricate release notes from a guess.  The
# warning below is the signal that wants fixing.
_FALLBACK_TYPE = "chore"

_VALID_TYPES = frozenset(
    {
        "feat",
        "fix",
        "docs",
        "chore",
        "refactor",
        "perf",
        "test",
        "build",
        "ci",
        "style",
        "revert",
    }
)

# ``Change-Type: fix`` — the line the implement agent is asked to put in
# its summary.  Tolerates a leading bullet, bold markers and backticks.
_CHANGE_TYPE_RE = re.compile(
    r"^[\s\-*]*\**change[-_ ]type\**\s*:\**\s*`?(?P<type>[a-z]+)`?",
    re.IGNORECASE | re.MULTILINE,
)


def type_from_summary(summary: str | None) -> str | None:
    """Return the conventional type the implement agent declared, if any."""
    if not summary:
        return None
    m = _CHANGE_TYPE_RE.search(summary)
    if m is None:
        return None
    declared = m.group("type").lower()
    if declared not in _VALID_TYPES:
        log.warning("unrecognised Change-Type %r in the implement summary", declared)
        return None
    return declared


# Where the type is parked for the deliver stage, which builds the PR
# title long after the implement summary has been consumed.
_TYPE_ARTIFACT = "change_type.txt"


def record_type(artifacts_dir: Path | None, ctype: str | None) -> None:
    """Park *ctype* so deliver can classify the PR title later."""
    if artifacts_dir is None or not ctype:
        return
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / _TYPE_ARTIFACT).write_text(ctype + "\n", encoding="utf-8")
    except OSError:
        log.warning("could not record the change type", exc_info=True)


def _recorded_type(artifacts_dir: Path | None) -> str | None:
    if artifacts_dir is None:
        return None
    try:
        return (artifacts_dir / _TYPE_ARTIFACT).read_text(
            encoding="utf-8"
        ).strip() or None
    except OSError:
        return None


def _already_conventional(title: str) -> bool:
    """Return True when *title* already opens with a conventional type."""
    head, sep, _ = title.partition(":")
    if not sep:
        return False
    head = head.strip()
    head = head.removesuffix("!")
    if head.endswith(")") and "(" in head:
        head = head[: head.index("(")]
    return head in _VALID_TYPES


def conventional_subject(
    ticket_id: str,
    title: str,
    *,
    suffix: str = "",
    artifacts_dir: Path | None = None,
) -> str:
    """Build the commit/PR subject for a mill-authored change.

    The result is ``<type>: <title> (<ticket_id>)`` so release-please
    picks the change up, while the ticket id stays in the subject —
    several mill stages locate a squash-merged branch by grepping the
    target branch's log for it.
    """
    title = title.strip()
    if _already_conventional(title):
        # A refine-authored title that already carries a type is
        # authoritative; prefixing a second one would break parsing.
        return f"{title} ({ticket_id}){suffix}"

    ctype = _recorded_type(artifacts_dir)
    if ctype is None or ctype not in _VALID_TYPES:
        log.warning(
            "%s: no conventional type in the title and no Change-Type recorded "
            "by implement — falling back to '%s:', so this change will not "
            "appear in CHANGELOG.md or bump the version",
            ticket_id,
            _FALLBACK_TYPE,
        )
        ctype = _FALLBACK_TYPE

    return f"{ctype}: {title} ({ticket_id}){suffix}"
