"""Filesystem + shell tools for the implement agent, sandboxed to one
repo directory.

Every path is resolved and checked to stay inside ``root`` so the agent
cannot read or write outside the ticket's clone. ``run_command`` executes
with ``cwd=root`` and a hard timeout. Tools are plain closures with type
hints + docstrings — pydantic-ai derives the schema from those.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .. import sandbox


def _safe(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    root = root.resolve()
    if p != root and not p.is_relative_to(root):
        raise ValueError(f"path {rel!r} escapes the repository")
    return p


def build_fs_tools(root: Path, settings: Settings) -> list:
    root = Path(root).resolve()

    # Tools return errors as strings so the model can self-correct
    # (try another path, list the dir, ...) instead of the whole agent
    # run aborting on an exception.
    def read_file(path: str) -> str:
        """Return the text content of a file in the repository."""
        try:
            return _safe(root, path).read_text(
                encoding="utf-8", errors="replace"
            )
        except (ValueError, OSError) as e:
            return f"error: {e}"

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file in the repository with ``content``."""
        try:
            p = _safe(root, path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except (ValueError, OSError) as e:
            return f"error: {e}"
        return f"wrote {len(content)} bytes to {path}"

    def list_dir(path: str = ".") -> str:
        """List entries of a directory in the repository (dirs end '/')."""
        try:
            d = _safe(root, path)
            return "\n".join(
                sorted(
                    f"{e.name}/" if e.is_dir() else e.name
                    for e in d.iterdir()
                )
            )
        except (ValueError, OSError) as e:
            return f"error: {e}"

    def run_command(command: str) -> str:
        """Run a shell command against the repository (tests, linters,
        build steps, generators, ...). Returns exit code + combined
        stdout/stderr (truncated). Runs in an isolated, network-less
        sandbox — no internet, nothing outside the repo is reachable."""
        try:
            rc, out = sandbox.run(command, repo_dir=root, settings=settings)
        except sandbox.SandboxError as e:
            return f"sandbox error: {e}"
        return f"exit={rc}\n{out}"

    return [read_file, write_file, list_dir, run_command]
