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
    def repo_dir(self) -> Path:
        """Path to the ``repo/`` subdirectory (no creation side-effect)."""
        return self.dir / "repo"

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
        return hashlib.sha256(
            self.description_path.read_bytes()
        ).hexdigest()


def link_epic_workspace(repo_dir: Path, epic_workspace_path: Path) -> bool:
    """Create or refresh a symlink at ``repo_dir/_epic`` →
    ``epic_workspace_path``.

    Returns ``True`` when the symlink was created (or already existed and
    was refreshed).  Returns ``False`` when ``_epic/`` exists as a real
    directory (not a symlink) — the repo tracks its own ``_epic/`` and
    we must not overwrite it.  Callers should skip ALL epic-workspace
    wiring (sandbox mount, extra_roots, prompt note) when this returns
    ``False``.
    """
    link = repo_dir / "_epic"
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        # Don't overwrite a real _epic/ directory that belongs to the repo
        return False
    link.symlink_to(epic_workspace_path)
    return True


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
        log.warning(
            "prune_clone: could not remove %s – continuing", repo
        )
