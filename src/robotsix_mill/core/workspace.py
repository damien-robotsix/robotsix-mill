"""Per-ticket filesystem workspace (the work plane).

File-canonical: ``description.md`` is the source of truth for the ticket
body — agents read and rewrite it directly. The DB only stores the path
and a content hash so the management plane can detect external edits.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("robotsix_mill.core.workspace")


class Workspace:
    """Per-ticket directory layout providing access to ``description.md``, ``artifacts/``, and ``repo/``."""

    def __init__(self, root: Path, ticket_id: str) -> None:
        """Create the workspace directory for *ticket_id* under *root*, creating parents as needed."""
        # Defend against path-injection: ticket_id must be a simple leaf name.
        if ticket_id != Path(ticket_id).name or ticket_id in (".", ".."):
            raise ValueError(f"Unsafe ticket_id: {ticket_id!r}")
        # Containment check in the realpath + prefix form so static
        # analysis (CodeQL py/path-injection) recognises the barrier;
        # the leaf-name check above already rejects separators, so this
        # only guards against exotic resolution edge cases.
        _root = os.path.realpath(os.fspath(root))
        _dir = os.path.realpath(os.path.join(_root, ticket_id))
        if not _dir.startswith(_root + os.sep):
            raise ValueError(f"Unsafe ticket_id: {ticket_id!r}")
        self.dir = Path(_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def description_path(self) -> Path:
        """Path to ``description.md`` — the canonical ticket body."""
        return self.dir / "description.md"

    @property
    def artifacts_dir(self) -> Path:
        """Path to the ``artifacts/`` subdirectory, creating it lazily on first access."""
        d = self.dir / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def screenshots_dir(self) -> Path:
        """Path to the ``screenshots/`` subdirectory, creating it lazily on first access.

        Kept as a sibling of ``artifacts/`` (not under it) so user-supplied
        screenshots survive a refine restart-from-scratch, which wipes
        ``artifacts/`` but must preserve user input.
        """
        d = self.dir / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def repo_dir(self) -> Path:
        """Path to the ``repo/`` subdirectory (no creation side-effect)."""
        return self.dir / "repo"

    def list_screenshots(self) -> list[Path]:
        """Return stored screenshot image files, sorted by name for determinism.

        Returns ``[]`` when the ``screenshots/`` directory is absent. Only files
        with a supported image extension (``.png``, ``.jpg``, ``.jpeg``,
        ``.gif``, ``.webp``) are included.
        """
        d = self.dir / "screenshots"
        if not d.exists():
            return []
        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        return sorted(
            (p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts),
            key=lambda p: p.name,
        )

    def write_description(self, text: str) -> str:
        """Write *text* to ``description.md`` and return the new content hash."""
        self.description_path.write_text(text, encoding="utf-8")
        return self.content_hash()

    def read_description(self) -> str:
        """Return the text of ``description.md``, or an empty string if absent."""
        if not self.description_path.exists():
            return ""
        return self.description_path.read_text(encoding="utf-8")

    def content_hash(self) -> str:
        """Return the SHA-256 hex digest of ``description.md``, or an empty string if absent."""
        if not self.description_path.exists():
            return ""
        return hashlib.sha256(self.description_path.read_bytes()).hexdigest()


def read_counter(path: Path) -> int:
    """Read an integer from *path*, returning 0 when the file is missing or unparseable."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError, ValueError:
        return 0


def write_counter(path: Path, value: int) -> None:
    """Write *value* to *path*, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


# --- spawn-exhaustion recurrence marker --------------------------------
# Persists ``{spec_fp, count}`` in the ticket's artifacts dir so the
# implement preflight and the resume-blocked transition can agree on
# whether this ticket has ALREADY exhausted its spawn budget on the
# current spec fingerprint.  ``spec_fp`` is the same 16-hex effective
# spec fingerprint used by the stale-re-spawn guard (SHA-256 of epic
# context + description, truncated).  The marker survives counter
# resets — it is cleared only on a spec change (preflight rewrites
# it), on productive implement progress, or on an explicit operator
# rework request (`_reset_implement_spawn_counter`).

SPAWN_EXHAUSTION_MARKER = "implement_spawn_exhausted.json"


def read_spawn_exhaustion_marker(ws: Workspace) -> tuple[str, int] | None:
    """Return ``(spec_fp, count)`` from the spawn-exhaustion marker.

    Returns ``None`` when the marker is absent, malformed, or carries
    an empty fingerprint / non-positive count.
    """
    path = ws.artifacts_dir / SPAWN_EXHAUSTION_MARKER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        spec_fp = str(data.get("spec_fp", "")).strip()
        count = int(data.get("count", 0))
    except OSError, ValueError, TypeError, json.JSONDecodeError:
        return None
    if not spec_fp or count < 1:
        return None
    return spec_fp, count


def record_spawn_exhaustion_marker(ws: Workspace, spec_fp: str, count: int) -> None:
    """Write the spawn-exhaustion marker with the given fingerprint and count.

    Creates the artifacts dir as needed.  An ``OSError`` propagates —
    callers treat marker writes as best-effort (the event emission is
    already fail-safe).
    """
    path = ws.artifacts_dir / SPAWN_EXHAUSTION_MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"spec_fp": spec_fp, "count": count}, ensure_ascii=False),
        encoding="utf-8",
    )


def clear_spawn_exhaustion_marker(ws: Workspace) -> None:
    """Delete the spawn-exhaustion marker; silent when absent."""
    with contextlib.suppress(OSError):
        (ws.artifacts_dir / SPAWN_EXHAUSTION_MARKER).unlink(missing_ok=True)


def prune_clone(workspace: Workspace) -> None:
    """Delete the ``repo/`` subdirectory of *workspace*.

    Idempotent – silently succeeds when ``repo/`` is absent.
    Best-effort – any ``OSError`` is logged and swallowed; the caller
    continues as if pruning succeeded.
    """
    repo = workspace.repo_dir
    try:
        shutil.rmtree(repo, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("prune_clone: could not remove %s – continuing", repo)
