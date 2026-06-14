"""``validate_artifact`` agent tool — deterministic, clone-scoped
existence check for a single repository-relative path.

Periodic detectors that propose draft tickets about MISSING artifacts
(e.g. "add unit tests for ``<module>``") can hallucinate paths that do
not exist in the clone, filing false-positive gaps. This tool gives the
agent a crisp, network-free primitive to confirm a path before filing:
it reports whether *path* exists inside the cloned repo, and refuses to
resolve outside it.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable


def validate_artifact_path(repo_dir: Path, path: str) -> str:
    """Report whether *path* exists inside *repo_dir*.

    *path* is interpreted relative to *repo_dir*. The result is a short,
    deterministic, human-readable string the LLM can act on:

    - ``"EXISTS: <path> (file)"`` — resolves to a regular file.
    - ``"EXISTS: <path> (directory)"`` — resolves to a directory.
    - ``"MISSING: <path> does not exist in the repository"`` — no such
      path, or a path that escapes *repo_dir*.

    The filesystem check (``(repo_dir / path).exists()``) is
    authoritative and never touches the network. A path that resolves
    outside *repo_dir* is reported as MISSING rather than traversed, so
    the tool can never read outside the clone. Never raises on a missing
    path.
    """
    root = Path(repo_dir).resolve()
    candidate = (root / path).resolve()

    # Confine to the clone: a path escaping ``repo_dir`` is treated as
    # not existing, never traversed outside the checkout.
    if candidate != root and not candidate.is_relative_to(root):
        return f"MISSING: {path} does not exist in the repository"

    if candidate.is_dir():
        return f"EXISTS: {path} (directory)"
    if candidate.exists():
        return f"EXISTS: {path} (file)"
    return f"MISSING: {path} does not exist in the repository"


def make_validate_artifact_tool(repo_dir: Path) -> Callable[[str], str]:
    """Create the ``validate_artifact`` tool closure.

    Follows the same factory pattern as ``make_jscpd_tool``: wraps the
    existence check in a closure bound to *repo_dir* and self-registers
    into ``ToolRegistry``.
    """

    def validate_artifact(path: str) -> str:
        """Deterministically check whether a repository-relative path
        exists in the cloned repo, returning ``EXISTS: <path> (file)``,
        ``EXISTS: <path> (directory)``, or ``MISSING: <path> ...``. Use
        this to confirm an artifact's path before filing a draft about
        it; never resolves outside the clone."""
        return validate_artifact_path(repo_dir, path)

    from .tool_registry import ToolInfo, ToolRegistry

    if not any(t.name == "validate_artifact" for t in ToolRegistry.list_tools()):
        ToolRegistry.register(
            ToolInfo(
                name="validate_artifact",
                description=(
                    "Deterministically check whether a repository-relative "
                    "path exists in the cloned repo, returning an EXISTS or "
                    "MISSING string. Confined to the clone — never resolves "
                    "outside it."
                ),
                category="exploration",
                parameters={"path": "str"},
            )
        )

    return validate_artifact
