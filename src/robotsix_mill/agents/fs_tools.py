"""Filesystem + shell tools for the implement agent, sandboxed to one
repo directory.

Every path is resolved and checked to stay inside ``root`` so the agent
cannot read or write outside the ticket's clone. ``run_command`` executes
with ``cwd=root`` and a hard timeout. Tools are plain closures with type
hints + docstrings — pydantic-ai derives the schema from those.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic_ai import RunContext

from ..config import Settings
from .. import sandbox

log = logging.getLogger(__name__)

# Placeholder swapped in for a now-stale read_file result in the live
# pydantic-ai message history once the same file is read fresh again.
_PRUNED_PLACEHOLDER = "[content pruned — more recent content above]"


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


def build_fs_tools(root: Path, settings: Settings, *, pre_seeded: dict[Path, str] | None = None) -> list:
    root = Path(root).resolve()

    # In-memory file-content cache shared by all closures in this
    # build_fs_tools call.  Lifetime = one agent invocation.
    _file_cache: dict[Path, str] = {}

    if pre_seeded:
        _file_cache.update(pre_seeded)

    def _read_cached(p: Path) -> str:
        """Read *p* and cache the result.  *p* is already sandbox-safe
        (returned by ``_safe``), but we ``resolve()`` again for a
        canonical cache key.  ``ValueError`` / ``OSError`` are re-raised
        so the caller can convert them to error strings."""
        key = p.resolve()
        if key in _file_cache:
            return _file_cache[key]
        text = p.read_text(encoding="utf-8", errors="replace")
        _file_cache[key] = text
        return text

    def _prune_stale_file_content(
        ctx: RunContext[None], current_path: Path
    ) -> None:
        """Replace earlier ``read_file`` results for *current_path* in the
        live pydantic-ai message history with a short placeholder.

        A fresh full-file read makes every prior full-content copy of the
        same file redundant — they only bloat the context window. This
        rewrites those stale ``ToolReturnPart`` contents in place; the
        small ``ToolCallPart``s are left untouched so pydantic-ai's
        turn-boundary invariants stay valid.

        Best-effort: pruning is a pure optimisation, so any failure is
        swallowed rather than allowed to break the agent run.
        """
        try:
            messages = getattr(ctx, "messages", None)
            if not messages:
                return
            current_canonical = current_path.resolve()

            # Pass 1 — tool_call_ids of earlier read_file calls for this file.
            stale_ids: set[str] = set()
            for msg in messages:
                if getattr(msg, "kind", None) != "response":
                    continue
                for part in getattr(msg, "parts", []):
                    if getattr(part, "part_kind", None) != "tool-call":
                        continue
                    if getattr(part, "tool_name", None) != "read_file":
                        continue
                    try:
                        arg_path = part.args_as_dict().get("path")
                        if not isinstance(arg_path, str):
                            continue
                        resolved = (root / arg_path).resolve()
                    except Exception:
                        continue
                    if resolved == current_canonical:
                        tool_call_id = getattr(part, "tool_call_id", None)
                        if tool_call_id:
                            stale_ids.add(tool_call_id)

            if not stale_ids:
                return

            # Pass 2 — replace the matching read_file ToolReturnPart contents.
            for msg in messages:
                if getattr(msg, "kind", None) != "request":
                    continue
                for part in getattr(msg, "parts", []):
                    if getattr(part, "part_kind", None) != "tool-return":
                        continue
                    if getattr(part, "tool_name", None) != "read_file":
                        continue
                    if getattr(part, "tool_call_id", None) in stale_ids:
                        part.content = _PRUNED_PLACEHOLDER
        except Exception:
            log.debug(
                "read_file: message-history pruning skipped", exc_info=True
            )

    # Tools return errors as strings so the model can self-correct
    # (try another path, list the dir, ...) instead of the whole agent
    # run aborting on an exception.
    def read_file(
        ctx: RunContext[None] = None,
        *,
        path: str,
        offset: int = 1,
        limit: int | None = None,
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
            p = _safe(root, path)
        except (ValueError, OSError) as e:
            return f"error: {e}"

        # Normalize offset for the stub check (offset ≤ 0 is treated as 1).
        _offset = offset if offset >= 1 else 1
        is_full_read = _offset == 1 and limit is None

        # Full-file stub: only when offset=1 AND limit=None (the common case).
        if is_full_read and p.resolve() in _file_cache:
            return "already in context above — unchanged"

        # Otherwise: read (or refresh) via _read_cached, then slice.
        try:
            text = _read_cached(p)
        except (ValueError, OSError) as e:
            return f"error: {e}"

        # A fresh full-file read (cache miss) makes every earlier copy of
        # this file in the message history redundant — prune them so the
        # stale content stops costing context-window tokens.
        if ctx is not None and is_full_read:
            _prune_stale_file_content(ctx, p)

        lines = text.splitlines(keepends=True)
        if _offset > len(lines) and _offset > 1:
            return f"(file has {len(lines)} lines; offset {offset} is beyond end)"
        start = _offset - 1
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
        _file_cache.pop(p.resolve(), None)
        return f"wrote {len(content)} bytes to {path}"

    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace a unique string in a file. Reads the file, locates
        ``old_string``, and if it appears exactly once replaces it with
        ``new_string``.  Returns a short result string — prefer this
        for surgical edits over ``write_file``."""
        try:
            p = _safe(root, path)
            content = _read_cached(p)
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
            _file_cache.pop(p.resolve(), None)
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
        _file_cache.pop(p.resolve(), None)
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
        sandbox — no internet, nothing outside the repo is reachable.

        Commands automatically execute in the repository root
        directory — do not prefix with ``cd /home/user/repo`` or
        similar. Use ``cd <subdir> && …`` to work in a subdirectory."""
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
        description=f"Run a shell command against the repository (tests, linters, build steps, generators, ...). Commands already execute in the repository root ({root}) — do NOT prefix with ``cd /home/user/repo``, ``cd /root/workspace``, or any other absolute cd to a repo root. Use ``cd <subdir> && …`` to work in a subdirectory.",
        category="shell",
        parameters={"command": "str"},
    ))

    return [read_file, write_file, edit_file, delete_file, list_dir, run_command]
