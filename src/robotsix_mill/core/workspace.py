"""Per-ticket filesystem workspace (the work plane).

File-canonical: ``description.md`` is the source of truth for the ticket
body — agents read and rewrite it directly. The DB only stores the path
and a content hash so the management plane can detect external edits.
"""

from __future__ import annotations

import hashlib
import logging
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
        self.dir = Path(root) / ticket_id
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
