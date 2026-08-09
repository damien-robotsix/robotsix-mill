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

The classification is not guessed here.  The implement agent already
decides what kind of change it made when it writes a towncrier
fragment named ``<ticket_id>.<kind>.md``; this module maps that kind
onto the conventional type and reuses it.  No extra LLM call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ._changelog_validate import _FRAGMENT_DIRS

log = logging.getLogger(__name__)

# towncrier fragment kind -> conventional-commit type.
#
# ``removal`` and ``deprecation`` describe user-visible changes to the
# public surface, so they map to ``feat`` to stay in the changelog and
# earn a bump; release-please has no dedicated type for them.
# ``security`` maps to ``fix`` for the same reason — a security change
# that never reaches the changelog is the worst one to lose.
_KIND_TO_TYPE = {
    "feature": "feat",
    "bugfix": "fix",
    "security": "fix",
    "removal": "feat",
    "deprecation": "feat",
    "doc": "docs",
    "misc": "chore",
}

# Used when no fragment is present.  Deliberately a type that neither
# bumps the version nor appears in the changelog: a missing fragment
# means the implement agent skipped a required step, and inventing a
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


def fragment_kind(repo_dir: Path, ticket_id: str) -> str | None:
    """Return the towncrier kind the implement agent chose, if any.

    Fragments are named ``<ticket_id>.<kind>.md``.  When the agent wrote
    more than one, the highest-priority kind wins, matching the
    de-duplication order the ci_fix stage already uses.
    """
    kinds: list[str] = []
    for name in _FRAGMENT_DIRS:
        directory = repo_dir / name
        if not directory.is_dir():
            continue
        for frag in sorted(directory.glob(f"{ticket_id}.*.md")):
            parts = frag.name.split(".")
            if len(parts) >= 3:
                kinds.append(parts[-2])

    if not kinds:
        return None
    # Rank by the mapped type so the most release-significant kind wins:
    # feat > fix > docs > chore, then alphabetically for stability.
    rank = {"feat": 0, "fix": 1, "docs": 2, "chore": 3}
    return min(
        kinds,
        key=lambda k: (rank.get(_KIND_TO_TYPE.get(k, _FALLBACK_TYPE), 4), k),
    )


# Where the kind is parked for the deliver stage.  Implement deletes the
# fragment from the branch, so by the time deliver builds the PR title the
# original evidence is gone and every PR would fall back to ``chore``.
_KIND_ARTIFACT = "changelog_kind.txt"


def record_kind(artifacts_dir: Path | None, kind: str | None) -> None:
    """Park *kind* so deliver can still classify after the fragment is gone."""
    if artifacts_dir is None or not kind:
        return
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / _KIND_ARTIFACT).write_text(kind + "\n", encoding="utf-8")
    except OSError:
        log.warning("could not record the changelog kind", exc_info=True)


def _recorded_kind(artifacts_dir: Path | None) -> str | None:
    if artifacts_dir is None:
        return None
    try:
        return (artifacts_dir / _KIND_ARTIFACT).read_text(
            encoding="utf-8"
        ).strip() or None
    except OSError:
        return None


def resolve_kind(
    repo_dir: Path, ticket_id: str, artifacts_dir: Path | None = None
) -> str | None:
    """The fragment when it is still on the branch, else what implement parked."""
    return fragment_kind(repo_dir, ticket_id) or _recorded_kind(artifacts_dir)


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
    repo_dir: Path,
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

    kind = resolve_kind(repo_dir, ticket_id, artifacts_dir)
    if kind is None:
        log.warning(
            "%s: no changelog fragment or recorded kind for %s — falling back "
            "to '%s:', "
            "so this change will not appear in CHANGELOG.md or bump the "
            "version",
            ticket_id,
            repo_dir,
            _FALLBACK_TYPE,
        )
        ctype = _FALLBACK_TYPE
    else:
        ctype = _KIND_TO_TYPE.get(kind, _FALLBACK_TYPE)
        if kind not in _KIND_TO_TYPE:
            log.warning(
                "%s: unrecognised changelog kind %r — using '%s:'",
                ticket_id,
                kind,
                _FALLBACK_TYPE,
            )

    return f"{ctype}: {title} ({ticket_id}){suffix}"


def drop_fragments(repo_dir: Path, ticket_id: str) -> list[Path]:
    """Delete this ticket's fragments from a release-please repo.

    Once the kind has been folded into the commit subject the fragment
    has no consumer: release-please builds the changelog from commits,
    and nothing drains ``changelog.d`` any more.  Left in place the
    files accumulate on ``main`` as dead duplicates of the changelog.

    No-op on a repo that has not migrated.
    """
    if not (repo_dir / "release-please-config.json").is_file():
        return []

    removed: list[Path] = []
    for name in _FRAGMENT_DIRS:
        directory = repo_dir / name
        if not directory.is_dir():
            continue
        for frag in sorted(directory.glob(f"{ticket_id}.*.md")):
            try:
                frag.unlink()
            except OSError:
                log.warning("%s: could not remove %s", ticket_id, frag, exc_info=True)
                continue
            removed.append(frag)
        # Drop the directory too once this ticket emptied it, so the
        # migration's removal is not quietly undone.
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass

    _unclaim(repo_dir, removed)
    return removed


def _unclaim(repo_dir: Path, removed: list[Path]) -> None:
    """Drop deleted fragments from ``docs/modules.yaml``.

    The implement agent registers the fragment it writes, so deleting the
    file without the claim leaves the registry pointing at nothing and the
    drift check fails with "matches no files on disk" — turning a cleanup
    into a red PR.
    """
    if not removed:
        return
    modules = repo_dir / "docs" / "modules.yaml"
    if not modules.is_file():
        return

    names = {p.name for p in removed}
    try:
        lines = modules.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        log.warning("could not read %s", modules, exc_info=True)
        return

    kept = [
        line
        for line in lines
        if not (line.lstrip().startswith("- ") and any(n in line for n in names))
    ]
    if len(kept) == len(lines):
        return
    try:
        modules.write_text("".join(kept), encoding="utf-8")
    except OSError:
        log.warning("could not rewrite %s", modules, exc_info=True)
        return
    log.info(
        "unclaimed %d fragment path(s) from docs/modules.yaml", len(lines) - len(kept)
    )
