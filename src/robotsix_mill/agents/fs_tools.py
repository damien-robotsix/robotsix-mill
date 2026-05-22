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
    if not root.exists():
        raise ValueError(
            "workspace repo directory does not exist — "
            "the repository has not been cloned yet"
        )
    return p


def build_fs_tools(root: Path, settings: Settings) -> list:
    root = Path(root).resolve()

    # Tools return errors as strings so the model can self-correct
    # (try another path, list the dir, ...) instead of the whole agent
    # run aborting on an exception.
    def read_file(
        path: str, offset: int = 1, limit: int | None = None
    ) -> str:
        """Return the text content of a file in the repository.

        Optionally narrow to a line range with ``offset`` and ``limit``
        (both 1-indexed):
        - ``offset`` — first line to return (default 1). Values ≤ 0 are
          treated as 1.
        - ``limit`` — maximum number of lines to return (None = to end).
          When ``limit`` exceeds remaining lines, returns what's available.
        - If ``offset`` is past the last line, returns a note with the
          actual line count.
        """
        try:
            text = _safe(root, path).read_text(
                encoding="utf-8", errors="replace"
            )
        except (ValueError, OSError) as e:
            return f"error: {e}"

        lines = text.splitlines(keepends=True)

        if offset < 1:
            offset = 1

        if offset > len(lines) and offset > 1:
            return f"(file has {len(lines)} lines; offset {offset} is beyond end)"

        start = offset - 1
        end = start + limit if limit is not None else None
        return "".join(lines[start:end])

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file in the repository with ``content``."""
        try:
            p = _safe(root, path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except (ValueError, OSError) as e:
            return f"error: {e}"
        return f"wrote {len(content)} bytes to {path}"

    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace a unique string in a file. Reads the file, locates
        ``old_string``, and if it appears exactly once replaces it with
        ``new_string``.  Returns a short result string — prefer this
        for surgical edits over ``write_file``."""
        try:
            p = _safe(root, path)
            content = p.read_text(encoding="utf-8", errors="replace")
            count = content.count(old_string)
            if count == 0:
                return (
                    f"edit_file: old_string not found in {path} "
                    f"— read the file and retry, or use write_file"
                )
            if count > 1:
                return (
                    f"edit_file: old_string appears {count} times "
                    f"in {path} (must be unique) — read the file and "
                    f"retry, or use write_file"
                )
            p.write_text(
                content.replace(old_string, new_string, 1),
                encoding="utf-8",
            )
            return f"edit_file: replaced 1 occurrence in {path}"
        except (ValueError, OSError) as e:
            return f"error: {e}"

    def delete_file(path: str) -> str:
        """Delete a file from the repository. Returns a short status string."""
        try:
            p = _safe(root, path)
            p.unlink()
        except (ValueError, OSError) as e:
            return f"error: {e}"
        return f"deleted {path}"

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
        if not root.exists():
            return (
                "error: workspace repo directory does not exist — "
                "the repository has not been cloned yet"
            )
        try:
            rc, out = sandbox.run(command, repo_dir=root, settings=settings)
        except sandbox.SandboxError as e:
            return f"sandbox error: {e}"
        return f"exit={rc}\n{out}"

    # Register every fs/shell tool in the system-wide capability catalog.
    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(ToolInfo(
        name="read_file",
        description="Return the text content of a file in the repository.",
        category="fs",
        parameters={"path": "str", "offset": "int = 1", "limit": "int | None = None"},
    ))
    ToolRegistry.register(ToolInfo(
        name="write_file",
        description="Create or overwrite a file in the repository with ``content``.",
        category="fs",
        parameters={"path": "str", "content": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="edit_file",
        description="Replace a unique string in a file.",
        category="fs",
        parameters={"path": "str", "old_string": "str", "new_string": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="delete_file",
        description="Delete a file from the repository.",
        category="fs",
        parameters={"path": "str"},
    ))
    ToolRegistry.register(ToolInfo(
        name="list_dir",
        description="List entries of a directory in the repository (dirs end '/').",
        category="fs",
        parameters={"path": "str = \".\""},
    ))
    ToolRegistry.register(ToolInfo(
        name="run_command",
        description="Run a shell command against the repository (tests, linters, build steps, generators, ...).",
        category="shell",
        parameters={"command": "str"},
    ))

    return [read_file, write_file, edit_file, delete_file, list_dir, run_command]
