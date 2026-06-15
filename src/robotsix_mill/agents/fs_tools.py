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


def build_preseed_history(
    repo_dir: Path,
    paths: list[str],
    *,
    user_prompt: str | None = None,
    max_files: int = 20,
    max_total_bytes: int = 200_000,
) -> list:
    """Build a synthetic ``message_history`` that pre-loads *paths*
    under *repo_dir* into the agent's context.

    All paths share a single turn: one ``ModelResponse`` carrying N
    ``ToolCallPart``s for ``read_file`` and one matching ``ModelRequest``
    carrying N ``ToolReturnPart``s with the contents. This is the
    parallel-tool-call shape every modern provider supports — the
    agent sees "I made N read_file calls in parallel and got N
    results in one batch" rather than N sequential one-call turns.

    When *user_prompt* is provided, a leading ``ModelRequest`` carrying
    the prompt as a ``UserPromptPart`` is prepended to the history.
    The caller must then invoke ``agent.run_sync(None,
    message_history=...)`` (or with a different continuation prompt).
    The resulting conversation reads cleanly:

        system → user (real prompt) → assistant (preload tool_calls)
        → user (tool returns) → assistant (model's actual response)

    Without this restructuring pydantic-ai bundles the new user_prompt
    as a trailing ``TextPart`` inside the same ``ModelRequest`` as the
    tool returns, so the Langfuse "Formatted" view hides it and the
    model sees its own request only AFTER the tool-call exchange.

    Returns an empty list when *paths* is empty AND *user_prompt* is None.

    Defensive checks:
    - Files that don't exist on disk are skipped (with a warning) so
      a deleted-file entry in the caller's list doesn't abort the run.
    - The combined payload is capped at *max_files* / *max_total_bytes*;
      paths past the cap are dropped (with a warning) so a huge diff
      can't blow the context window.

    Used by:
    - implement (``coordinating.py``) — preloads reference_files the
      refine agent curated.
    - review (``reviewing.py``) — preloads every file the implement
      stage actually modified.
    """
    if not paths and user_prompt is None:
        return []

    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    calls: list = []
    returns: list = []
    total_bytes = 0
    for path in paths:
        if len(calls) >= max_files:
            log.warning(
                "build_preseed_history: max_files=%d reached, dropping "
                "remaining paths: %s",
                max_files,
                paths[paths.index(path) :],
            )
            break
        file_path = repo_dir / path
        try:
            if file_path.suffix.lower() == ".pdf":
                content = _extract_pdf_text(file_path)
            else:
                content = file_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
        except OSError:
            log.warning(
                "build_preseed_history: %s not found on disk, skipping",
                path,
            )
            continue
        if total_bytes + len(content) > max_total_bytes:
            log.warning(
                "build_preseed_history: max_total_bytes=%d would be "
                "exceeded by %s (size %d, cumulative %d) — dropping "
                "this and remaining paths",
                max_total_bytes,
                path,
                len(content),
                total_bytes,
            )
            break
        total_bytes += len(content)
        tc_id = f"preload_{path}"
        calls.append(
            ToolCallPart(
                tool_name="read_file",
                args={"path": path, "offset": 1, "limit": None},
                tool_call_id=tc_id,
            )
        )
        returns.append(
            ToolReturnPart(
                tool_name="read_file",
                content=content,
                tool_call_id=tc_id,
            )
        )

    history: list = []
    if user_prompt is not None:
        history.append(
            ModelRequest(
                parts=[
                    UserPromptPart(content=user_prompt),
                ]
            )
        )
    if calls:
        history.append(ModelResponse(parts=calls))
        history.append(ModelRequest(parts=returns))
    return history


def _safe(root: Path, rel: str, *, extra_roots: list[Path] | None = None) -> Path:
    if not root.exists():
        raise ValueError(
            "workspace repo directory does not exist — "
            "the repository has not been cloned yet"
        )
    p = (root / rel).resolve()
    root = root.resolve()
    if p != root and not p.is_relative_to(root):
        # Allow paths that resolve into an extra root
        if extra_roots:
            for extra in extra_roots:
                if p.is_relative_to(extra.resolve()):
                    return p
        raise ValueError(
            f"path {rel!r} escapes the repository — all filesystem tools "
            "(read_file, list_dir, run_command) are sandboxed to the repo "
            "checkout, so files outside it (installed dependencies under "
            "site-packages, /usr/local/lib, /etc, other workspaces) are NOT "
            "reachable by any tool. Do NOT retry this path and do NOT fall "
            "back to run_command/grep/cat on it — it will fail the same way."
        )
    return p


def _extract_pdf_text(p: Path) -> str:
    """Extract the text layer from a PDF file using ``pypdf``.

    The import is lazy — ``pypdf`` is not loaded until the first
    ``.pdf`` file is actually read.
    """
    import pypdf

    try:
        reader = pypdf.PdfReader(str(p))
    except Exception as e:
        return f"error reading PDF {p}: {e}"

    if reader.is_encrypted:
        return "error: PDF is encrypted — cannot extract text without a password"

    texts: list[str] = []
    try:
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                texts.append(page_text)
    except Exception as e:
        return f"error reading PDF {p}: {e}"

    return "\n".join(texts)


def build_fs_tools(
    root: Path,
    settings: Settings,
    *,
    pre_seeded: dict[Path, str] | None = None,
    extra_roots: list[Path] | None = None,
    sandbox_image: str | None = None,
) -> list:
    """Build the filesystem + shell tool closures sandboxed to *root*.

    Returns the ``read_file``, ``write_file``, ``edit_file``,
    ``delete_file``, ``list_dir``, and ``run_command`` tools as plain
    closures (pydantic-ai derives each tool's schema from its type
    hints + docstring). Every path is resolved and confined to *root*
    (or an entry in *extra_roots*) so the agent cannot read or write
    outside the ticket's clone, and the tools share an in-memory
    file-content cache for the lifetime of this call. Each tool is also
    registered in the :class:`~.tool_registry.ToolRegistry` catalog.

    Args:
        root: The repository directory the tools are confined to.
        settings: Application configuration (controls e.g.
            ``lint_on_edit`` syntax checking and the ``run_command``
            sandbox).
        pre_seeded: Optional mapping of resolved ``Path`` to file
            content used to warm the shared read cache (e.g. the
            implement coordinator's reference files).
        extra_roots: Optional additional directories that resolved
            paths are also allowed to fall inside.
        sandbox_image: Optional per-repo sandbox image override forwarded
            to ``sandbox.run`` for the ``run_command`` tool. ``None`` →
            falls back to ``settings.sandbox_image``.

    Returns:
        A list of the six tool closures, in the order ``read_file``,
        ``write_file``, ``edit_file``, ``delete_file``, ``list_dir``,
        ``run_command``.
    """
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
        if p.suffix.lower() == ".pdf":
            text = _extract_pdf_text(p)
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
        _file_cache[key] = text
        return text

    def _prune_stale_file_content(ctx: RunContext[None], current_path: Path) -> None:
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
            log.debug("read_file: message-history pruning skipped", exc_info=True)

    def _file_already_in_history(ctx, target: Path) -> bool:
        """Return True if *target* is already in the live message
        history as a non-pruned full-file ``read_file`` return — either
        a preload (``tool_call_id`` starts with ``preload_``) or a
        prior runtime full read (``offset=1, limit=None``).

        Partial-slice prior reads do NOT count: a slice doesn't cover
        the whole file, so a later partial of a different range is
        legitimate. Pruned returns also do not count — their content
        is gone from context."""
        try:
            messages = getattr(ctx, "messages", None)
            if not messages:
                return False
            canonical = target.resolve()

            full_read_ids: set[str] = set()
            for msg in messages:
                if getattr(msg, "kind", None) != "response":
                    continue
                for part in getattr(msg, "parts", []):
                    if getattr(part, "part_kind", None) != "tool-call":
                        continue
                    if getattr(part, "tool_name", None) != "read_file":
                        continue
                    try:
                        args = part.args_as_dict()
                        arg_path = args.get("path")
                        if not isinstance(arg_path, str):
                            continue
                        if (root / arg_path).resolve() != canonical:
                            continue
                    except Exception:
                        continue
                    tc_id = getattr(part, "tool_call_id", None)
                    if not tc_id:
                        continue
                    offset = args.get("offset", 1) or 1
                    limit = args.get("limit")
                    if tc_id.startswith("preload_") or (offset == 1 and limit is None):
                        full_read_ids.add(tc_id)

            if not full_read_ids:
                return False

            for msg in messages:
                if getattr(msg, "kind", None) != "request":
                    continue
                for part in getattr(msg, "parts", []):
                    if getattr(part, "part_kind", None) != "tool-return":
                        continue
                    if getattr(part, "tool_name", None) != "read_file":
                        continue
                    if getattr(part, "tool_call_id", None) not in full_read_ids:
                        continue
                    content = getattr(part, "content", None)
                    if isinstance(content, str) and content != _PRUNED_PLACEHOLDER:
                        return True
            return False
        except Exception:
            log.debug(
                "read_file: history scan skipped",
                exc_info=True,
            )
            return False

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
        """⚠️  **BEFORE YOU CALL:** Check your conversation history
        first — if a full copy of *path*'s content is already visible
        (from a prior ``read_file`` return, or from the
        ``reference_files`` preload block at conversation start),
        scroll back and quote from it. Re-asking wastes a round-trip.

        **Verify the path exists before calling.** If you are guessing a
        path (e.g. you think a file lives under ``docs/`` but are not
        certain), call ``list_dir`` on the parent directory first to
        confirm.  A ``read_file`` call on a non-existent path wastes a
        round-trip — the tool returns an error string, and you must
        retry.

        **Partial slices of fully-loaded files are REFUSED.** The
        ``reference_files`` block at conversation start preloads every
        listed file in full — asking for a slice of one of those is
        always wrong. Similarly, any file you read in full earlier in
        the session already sits in context; requesting a slice will
        fail. Use the content you already have.

        Return the text content of a file in the repository.

        Optionally narrow to a line range with ``offset`` and
        ``limit`` (both 1-indexed):
        - ``offset`` — first line to return (default 1). Values ≤ 0
          are treated as 1.
        - ``limit`` — maximum number of lines to return (None = to
          end).  When ``limit`` exceeds remaining lines, returns
          what's available.
        - If ``offset`` is past the last line, returns a note with
          the actual line count.

        ``.pdf`` files are supported: text is extracted from the
        PDF's text layer via ``pypdf``.  Encrypted PDFs return an
        error string.  Scanned PDFs with no text layer return an
        empty string.
        """
        try:
            p = _safe(root, path, extra_roots=extra_roots)
        except (ValueError, OSError) as e:
            return f"error: {e}"

        if not p.is_file():
            if p.exists():
                # Path exists but is a directory.
                return f"error: {path!r} is a directory, not a file"
            # Path does not exist at all.
            parent = p.parent
            try:
                parent_hint = str(parent.relative_to(root))
            except ValueError:
                parent_hint = str(parent)
            return (
                f"error: {path!r} does not exist — "
                f"try list_dir('{parent_hint}') "
                f"to find the correct path"
            )

        # Normalize offset (offset ≤ 0 is treated as 1).
        _offset = offset if offset >= 1 else 1
        is_full_read = _offset == 1 and limit is None

        # Refuse partial slices when the file is already loaded in
        # full earlier in the conversation. Encourages the model to
        # use the content it already has instead of layering a slice
        # on top of a still-present preload (which doubles the token
        # cost of that file for every later iteration).
        if ctx is not None and not is_full_read and _file_already_in_history(ctx, p):
            # Read the file from disk (cached) to report its line count
            # so the agent knows how much content it already holds.
            # The file is guaranteed to exist at this point (p.is_file()
            # guard above).
            try:
                _text = _read_cached(p)
                line_count: int | str = _text.count("\n")
            except ValueError, OSError:
                line_count = "?"
            return (
                f"refused: {path} ({line_count} lines) is already loaded in full "
                f"earlier in this conversation — scroll back to find it. "
                f"Do not re-read a slice of a file you already have in full."
            )

        # Read (or refresh) via _read_cached, then slice.
        try:
            text = _read_cached(p)
        except (ValueError, OSError) as e:
            return f"error: {e}"

        # A fresh full-file read makes every earlier copy of this file in
        # the message history redundant. Pruning them rewrites the prefix —
        # which invalidates the upstream prompt cache on THIS turn only, but
        # permanently shrinks the context for all later turns (cache reads
        # still cost per-token), a net economy over a long agentic loop.
        if ctx is not None and is_full_read:
            _prune_stale_file_content(ctx, p)

        lines = text.splitlines(keepends=True)
        if _offset > len(lines) and _offset > 1:
            return f"(file has {len(lines)} lines; offset {offset} is beyond end)"
        start = _offset - 1
        end = start + limit if limit is not None else None
        return "".join(lines[start:end])

    def _check_python_syntax(path: str, content: str) -> str | None:
        """Return a short error string if *path* is .py and *content* has
        a syntax error; ``None`` otherwise. Skipped when
        ``settings.lint_on_edit`` is False so an operator can disable
        the guard for a misbehaving repo.

        Only ``compile`` is used — not ruff/pylint — because a real
        linter requires the full module graph and false-positives on
        work-in-progress edits. ``compile`` catches the kind of error
        that would otherwise waste a full test cycle (missing colon,
        unmatched paren, stray indent)."""
        if not path.endswith(".py"):
            return None
        if not settings.lint_on_edit:
            return None
        try:
            compile(content, path, "exec")
        except SyntaxError as e:
            line = e.lineno or "?"
            return f"syntax error in {path} line {line}: {e.msg}"
        return None

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file in the repository with ``content``."""
        syntax_error = _check_python_syntax(path, content)
        if syntax_error is not None:
            return syntax_error
        try:
            p = _safe(root, path, extra_roots=extra_roots)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except (ValueError, OSError) as e:
            return f"error: {e}"
        _file_cache.pop(p.resolve(), None)
        return f"wrote {len(content)} bytes to {path}"

    def edit_file(path: str, old_string: str, new_string: str, count: int = 1) -> str:
        """Replace a unique string in a file. Reads the file, locates
        ``old_string``, and if it appears at least ``count`` times
        replaces the first ``count`` occurrences with ``new_string``.
        Returns a short result string — prefer this for surgical edits
        over ``write_file``."""
        try:
            p = _safe(root, path, extra_roots=extra_roots)
            content = _read_cached(p)
            occurrences = content.count(old_string)
            if occurrences == 0:
                return (
                    f"edit_file: old_string not found in {path} "
                    f"— read the file and retry, or use write_file"
                )
            if count == 1 and occurrences > 1 and old_string != "":
                return (
                    f"edit_file: old_string appears {occurrences} times "
                    f"in {path} — pass count={occurrences} to replace all, "
                    f"or a smaller count to replace fewer"
                )
            if occurrences < count:
                return (
                    f"edit_file: old_string appears {occurrences} time(s) "
                    f"in {path}, but {count} replacement(s) were requested "
                    f"— read the file and retry, or use write_file"
                )
            new_content = content.replace(old_string, new_string, count)
            syntax_error = _check_python_syntax(path, new_content)
            if syntax_error is not None:
                return syntax_error
            p.write_text(new_content, encoding="utf-8")
            _file_cache.pop(p.resolve(), None)
            return f"edit_file: replaced {count} occurrence(s) in {path}"
        except (ValueError, OSError) as e:
            return f"error: {e}"

    def delete_file(path: str) -> str:
        """Delete a file from the repository. Returns a short status string."""
        try:
            p = _safe(root, path, extra_roots=extra_roots)
            p.unlink()
        except (ValueError, OSError) as e:
            return f"error: {e}"
        _file_cache.pop(p.resolve(), None)
        return f"deleted {path}"

    def list_dir(path: str = ".") -> str:
        """List entries of a directory in the repository (dirs end '/')."""
        try:
            d = _safe(root, path, extra_roots=extra_roots)
            return "\n".join(
                sorted(f"{e.name}/" if e.is_dir() else e.name for e in d.iterdir())
            )
        except (ValueError, OSError) as e:
            return f"error: {e}"

    def run_command(command: str) -> str:
        """Run a shell command against the repository (tests, linters,
        build steps, generators, ...). Returns exit code + combined
        stdout/stderr (truncated). Runs in an isolated, network-less
        sandbox — no internet, nothing outside the repo is reachable.

        Commands automatically execute in the repository root
        directory — do not prefix with an absolute cd to a repo
        root. Use ``cd <subdir> && …`` to work in a subdirectory."""
        if not root.exists():
            return (
                "error: workspace repo directory does not exist — "
                "the repository has not been cloned yet"
            )
        try:
            rc, out = sandbox.run(
                command,
                repo_dir=root,
                settings=settings,
                sandbox_image=sandbox_image,
            )
        except sandbox.SandboxError as e:
            return f"sandbox error: {e}"
        if not out.strip():
            if rc == 0:
                return "Your command ran successfully and did not produce any output."
            return f"The command failed with exit code {rc} and produced no output."
        return f"exit={rc}\n{out}"

    # Register every fs/shell tool in the system-wide capability catalog.
    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="read_file",
            description="Return the text content of a file in the repository.",
            category="fs",
            parameters={
                "path": "str",
                "offset": "int = 1",
                "limit": "int | None = None",
            },
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="write_file",
            description="Create or overwrite a file in the repository with ``content``.",
            category="fs",
            parameters={"path": "str", "content": "str"},
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="edit_file",
            description="Replace a unique string in a file.",
            category="fs",
            parameters={
                "path": "str",
                "old_string": "str",
                "new_string": "str",
                "count": "int = 1",
            },
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="delete_file",
            description="Delete a file from the repository.",
            category="fs",
            parameters={"path": "str"},
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="list_dir",
            description="List entries of a directory in the repository (dirs end '/').",
            category="fs",
            parameters={"path": 'str = "."'},
        )
    )
    ToolRegistry.register(
        ToolInfo(
            name="run_command",
            description="Run a shell command against the repository (tests, linters, build steps, generators, ...).",
            category="shell",
            parameters={"command": "str"},
        )
    )

    return [read_file, write_file, edit_file, delete_file, list_dir, run_command]
