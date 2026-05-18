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
    def __init__(self, root: Path, ticket_id: str) -> None:
        self.dir = Path(root) / ticket_id
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def description_path(self) -> Path:
        return self.dir / "description.md"

    @property
    def artifacts_dir(self) -> Path:
        d = self.dir / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def repo_dir(self) -> Path:
        return self.dir / "repo"

    def write_description(self, text: str) -> str:
        self.description_path.write_text(text, encoding="utf-8")
        return self.content_hash()

    def read_description(self) -> str:
        if not self.description_path.exists():
            return ""
        return self.description_path.read_text(encoding="utf-8")

    def content_hash(self) -> str:
        if not self.description_path.exists():
            return ""
        return hashlib.sha256(
            self.description_path.read_bytes()
        ).hexdigest()


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
