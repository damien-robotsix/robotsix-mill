"""Filesystem + shell tools for the implement agent, sandboxed to one
repo directory.

Every path is resolved and checked to stay inside ``root`` so the agent
cannot read or write outside the ticket's clone. ``run_command`` executes
with ``cwd=root`` and a hard timeout. Tools are plain closures with type
hints + docstrings — pydantic-ai derives the schema from those.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from pydantic_ai import RunContext

from ..config import Settings
from ..core.repo_layout import src_path_candidates
from .. import sandbox
from ..runtime.tracing import trace_stage
from .periodic_loader import validate_periodic_file_content

log = logging.getLogger(__name__)

_PERIODIC_PATH_RE = re.compile(
    r"(^|[\\/])\.robotsix-mill[\\/]periodic[\\/][^\\/]+\.yaml$"
)

# Placeholder swapped in for a now-stale read_file result in the live
# pydantic-ai message history once the same file is read fresh again.
_PRUNED_PLACEHOLDER = "[content pruned — more recent content above]"


def _bound_full_read(text: str, max_chars: int) -> str:
    """Bound an *implicit full* ``read_file`` payload to *max_chars*.

    When ``text`` exceeds ``max_chars`` return a head slice + a tail
    slice (line-aligned) joined by an elision marker that states the
    file's total line count and steers the agent to re-read the omitted
    region with ``offset``/``limit``. Otherwise return ``text``
    unchanged. ``max_chars <= 0`` disables the guard (returns verbatim).

    This brings ``read_file`` up to the same output discipline
    ``run_command`` already has: a single tool return can't dump a
    290 KB lockfile into the prefix where it is re-billed on every
    later turn. The escape hatch (an explicit ranged read) is always
    available, so no content is ever unreachable.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    # Split the budget ~2/3 head, ~1/3 tail: the top of a file (imports,
    # signatures) is usually more orienting than the bottom.
    head_budget = (max_chars * 2) // 3
    tail_budget = max_chars - head_budget

    head_lines: list[str] = []
    used = 0
    for line in lines:
        if head_lines and used + len(line) > head_budget:
            break
        head_lines.append(line)
        used += len(line)

    tail_lines: list[str] = []
    used = 0
    for line in reversed(lines):
        if tail_lines and used + len(line) > tail_budget:
            break
        tail_lines.append(line)
        used += len(line)
    tail_lines.reverse()

    head_count = len(head_lines)
    tail_count = len(tail_lines)
    omitted_start = head_count + 1
    omitted_end = total_lines - tail_count
    marker = (
        f"\n\n[... read_file truncated: file has {total_lines} lines "
        f"({len(text)} chars), over the read_file_max_chars cap of "
        f"{max_chars}. Showing the first {head_count} and last "
        f"{tail_count} lines. To read the omitted region (lines "
        f"{omitted_start}-{omitted_end}), call read_file again with "
        f"offset/limit. ...]\n\n"
    )
    return "".join(head_lines) + marker + "".join(tail_lines)


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
    read_file_max_calls: int | None = None,
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
        read_file_max_calls: Optional per-run hard cap on ``read_file``
            calls. When set, the 1+read_file_max_calls th call returns an
            error string instead of reading the file. Default ``None``
            (unbounded). The counter is per ``build_fs_tools`` invocation.

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

    # Per-invocation counter for read_file hard-cap enforcement.
    # When read_file_max_calls is set, each read_file call increments
    # this counter; calls beyond the cap return an error string.
    _read_file_call_count: list[int] = [0]

    # Per-build accumulator of served read ranges for the closure-scoped
    # dedup guard on the Claude-SDK (ctx=None) path.  Keyed by resolved
    # path string (str(p.resolve())).  Each entry is a list of
    # (offset, limit) tuples recording every successfully-served range
    # for that path during this agent run.
    _served_reads: dict[str, list[tuple[int, int | None]]] = {}

    def _check_read_file_cap() -> str | None:
        """Return an error string if the read_file cap is exceeded,
        or None if the call should proceed."""
        if (
            read_file_max_calls is not None
            and _read_file_call_count[0] >= read_file_max_calls
        ):
            return (
                f"error: read_file hard cap of {read_file_max_calls} calls "
                f"reached for this agent run — no further files may be read. "
                f"Delegate remaining lookups to the explore tool, or "
                f"synthesise your spec from the content already in context."
            )
        _read_file_call_count[0] += 1
        return None

    def _record_served_read(resolved_path: str, offset: int, limit: int | None) -> None:
        """Record a successfully served (offset, limit) for *resolved_path*."""
        _served_reads.setdefault(resolved_path, []).append((offset, limit))

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

    def _find_covering_read(
        ctx: RunContext[None], target: Path, offset: int, limit: int | None
    ) -> tuple[int, int | None] | None:
        """Return ``(o, l)`` of the first non-pruned prior ``read_file``
        result for *target* whose line range fully contains the requested
        ``[offset, offset+limit)``, or ``None`` if no such read exists.

        A prior full read (``offset==1 and limit is None``, or a preload
        ``tool_call_id`` starting with ``preload_``) covers every line.
        A prior partial read with ``(o, l)`` covers ``[o, o+l)`` (or
        ``[o, EOF)`` when *l* is ``None`` but *o* > 1).  Pruned returns
        (content == ``_PRUNED_PLACEHOLDER``) are skipped — their content
        is gone from context."""
        try:
            messages = getattr(ctx, "messages", None)
            if not messages:
                return None
            canonical = target.resolve()

            # Pass 1 — collect (tool_call_id, (o, l)) for every
            # read_file call on this file.
            call_info: dict[str, tuple[int, int | None]] = {}
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
                    o = args.get("offset", 1) or 1
                    if o < 1:
                        o = 1
                    lim = args.get("limit")
                    call_info[tc_id] = (o, lim)

            if not call_info:
                return None

            # Compute the request's end (None limit → EOF / +inf).
            req_end = offset + limit if limit is not None else float("inf")

            # Pass 2 — find the first non-pruned return whose range
            # covers the request.
            for msg in messages:
                if getattr(msg, "kind", None) != "request":
                    continue
                for part in getattr(msg, "parts", []):
                    if getattr(part, "part_kind", None) != "tool-return":
                        continue
                    if getattr(part, "tool_name", None) != "read_file":
                        continue
                    tc_id = getattr(part, "tool_call_id", None)
                    if tc_id not in call_info:
                        continue
                    content = getattr(part, "content", None)
                    if not isinstance(content, str) or content == _PRUNED_PLACEHOLDER:
                        continue
                    # This return is live.  Check coverage.
                    o, lim = call_info[tc_id]
                    cov_end = o + lim if lim is not None else float("inf")
                    if offset >= o and req_end <= cov_end:
                        return (o, lim)
            return None
        except Exception:
            log.debug(
                "read_file: history scan skipped",
                exc_info=True,
            )
            return None

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

        To FIND where a symbol/pattern lives, use ``explore`` /
        ``parallel_explore``, or ``run_command`` with
        ``git grep <pattern>`` — do NOT read files one-by-one to
        search. Reserve ``read_file`` for files you have already
        identified as relevant.

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
        cap_error = _check_read_file_cap()
        if cap_error is not None:
            return cap_error

        resolved_note = ""
        try:
            p = _safe(root, path, extra_roots=extra_roots)
        except (ValueError, OSError) as e:
            # If the caller passed an absolute path that escapes,
            # try interpreting it as relative — agents sometimes
            # pass container-absolute paths (e.g. /workspace/...)
            # that don't exist on the host but whose tail names a
            # real repo file.
            if isinstance(e, ValueError) and path.startswith("/"):
                rel_tail = path[1:]
                if rel_tail:
                    try:
                        p = _safe(root, rel_tail, extra_roots=extra_roots)
                    except ValueError, OSError:
                        return f"error: {e}"
                    # Only accept the fallback if the relative form
                    # actually exists — otherwise the agent truly
                    # tried to escape and should get the escape error.
                    if not p.exists():
                        return f"error: {e}"
                    resolved_note = (
                        f"(resolved absolute {path!r} → relative {rel_tail!r}; "
                        f"use repo-relative paths)\n"
                    )
                else:
                    return f"error: {e}"
            else:
                return f"error: {e}"

        if not p.is_file():
            if p.exists():
                # Path exists but is a directory.
                return f"error: {path!r} is a directory, not a file"
            # Path does not exist at all — try src/ fallback before
            # returning the "does not exist" error.  This catches the
            # common agent mistake of probing e.g. "robotsix_llmio/core"
            # when the package actually lives under src/robotsix_llmio/core/.
            candidates = src_path_candidates(path)
            # Candidate 0 is the literal path — already tried above.
            for cand in candidates[1:]:
                try:
                    alt = _safe(root, cand, extra_roots=extra_roots)
                except ValueError, OSError:
                    continue
                if alt.is_file():
                    p = alt
                    resolved_note = (
                        f"(resolved {path!r} → {cand!r}: "
                        f"package paths live under the src/ namespace)\n"
                    )
                    break
            else:
                # No fallback candidate exists either — standard error.
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

        # Refuse partial slices when the file's content (or the
        # requested range) is already present earlier in the
        # conversation — either as a full-file read (including
        # preloads) or as a prior partial read whose line range
        # contains the request.  Encourages the model to use the
        # content it already has instead of layering a redundant
        # slice onto the still-present context.
        if ctx is not None and not is_full_read:
            covering = _find_covering_read(ctx, p, _offset, limit)
            if covering is not None:
                cov_o, cov_l = covering
                if cov_o == 1 and cov_l is None:
                    # Covered by a prior full read (or preload).
                    # Read the file from disk (cached) to report its
                    # line count so the agent knows how much content
                    # it already holds.  The file is guaranteed to
                    # exist at this point (p.is_file() guard above).
                    try:
                        _text = _read_cached(p)
                        line_count: int | str = _text.count("\n")
                    except ValueError, OSError:
                        line_count = "?"
                    return (
                        f"REFUSED (do NOT retry): {path} "
                        f"({line_count} lines) — already loaded in full "
                        f"earlier in this conversation.  Scroll back to "
                        f"find it; synthesise from context instead of "
                        f"re-reading."
                    )
                else:
                    # Covered by a prior partial read.
                    if cov_l is None:
                        cov_range = f"lines {cov_o} onward"
                    else:
                        cov_end_line = cov_o + cov_l - 1
                        cov_range = f"lines {cov_o}–{cov_end_line}"
                    return (
                        f"REFUSED (do NOT retry): {path} "
                        f"{cov_range} — already loaded earlier in this "
                        f"conversation.  Scroll back to find them; "
                        f"synthesise from context instead of re-reading."
                    )

        # Closure-scoped dedup for the Claude-SDK path (ctx is None).
        # The pydantic-ai path above scans message history; this path
        # consults the per-build accumulator of previously served ranges.
        # Coverage semantics match _find_covering_read.
        elif ctx is None:
            key = str(p.resolve())
            records = _served_reads.get(key, [])
            if records:
                req_end = _offset + limit if limit is not None else float("inf")
                for stored_offset, stored_limit in records:
                    cov_end = (
                        stored_offset + stored_limit
                        if stored_limit is not None
                        else float("inf")
                    )
                    if _offset >= stored_offset and req_end <= cov_end:
                        if stored_offset == 1 and stored_limit is None:
                            return (
                                f"REFUSED (do NOT retry): {path} "
                                f"— already loaded in full earlier in this "
                                f"conversation.  Scroll back to find it; "
                                f"synthesise from context instead of "
                                f"re-reading."
                            )
                        else:
                            if stored_limit is None:
                                cov_range = f"lines {stored_offset} onward"
                            else:
                                cov_end_line = stored_offset + stored_limit - 1
                                cov_range = f"lines {stored_offset}–{cov_end_line}"
                            return (
                                f"REFUSED (do NOT retry): {path} "
                                f"{cov_range} — already loaded earlier in "
                                f"this conversation.  Scroll back to find "
                                f"them; synthesise from context instead of "
                                f"re-reading."
                            )

        # Read (or refresh) via _read_cached, then slice.
        try:
            with trace_stage("read_file"):
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

        # Implicit full read: bound the payload so a large generated/
        # lock/baseline file can't dump its entire content into the
        # prefix (re-billed on every later tool turn). Explicit ranged
        # reads (offset > 1 or limit set) are never truncated here —
        # that escape hatch always retrieves any specific region.
        if is_full_read:
            _record_served_read(str(p.resolve()), _offset, limit)
            return resolved_note + _bound_full_read(text, settings.read_file_max_chars)

        lines = text.splitlines(keepends=True)
        if _offset > len(lines) and _offset > 1:
            return (
                resolved_note
                + f"(file has {len(lines)} lines; offset {offset} is beyond end)"
            )
        start = _offset - 1
        end = start + limit if limit is not None else None
        result = "".join(lines[start:end])
        _record_served_read(str(p.resolve()), _offset, limit)
        return resolved_note + result

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
        if _PERIODIC_PATH_RE.search(path):
            parsed: object = None
            try:
                parsed = yaml.safe_load(content)
            except yaml.YAMLError:
                pass
            if isinstance(parsed, dict):
                _name = parsed.get("name") or Path(path).stem
                _sp = parsed.get("system_prompt")
                _errs = validate_periodic_file_content(_name, _sp)
                if _errs:
                    return (
                        "PERIODIC FILE REJECTED — do not retry without fixing the issue:\n"
                        + "\n".join(f"  • {e}" for e in _errs)
                    )
        try:
            with trace_stage("write_file"):
                p = _safe(root, path, extra_roots=extra_roots)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        except (ValueError, OSError) as e:
            return f"error: {e}"
        _file_cache.pop(p.resolve(), None)
        _served_reads.pop(str(p.resolve()), None)
        return f"wrote {len(content)} bytes to {path}"

    def edit_file(path: str, old_string: str, new_string: str, count: int = 1) -> str:
        """Replace a unique string in a file. Reads the file, locates
        ``old_string``, and if it appears at least ``count`` times
        replaces the first ``count`` occurrences with ``new_string``.
        Returns a short result string — prefer this for surgical edits
        over ``write_file``."""
        try:
            with trace_stage("edit_file"):
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
                _served_reads.pop(str(p.resolve()), None)
                return f"edit_file: replaced {count} occurrence(s) in {path}"
        except (ValueError, OSError) as e:
            return f"error: {e}"

    def delete_file(path: str) -> str:
        """Delete a file from the repository. Returns a short status string."""
        try:
            with trace_stage("delete_file"):
                p = _safe(root, path, extra_roots=extra_roots)
                p.unlink()
        except (ValueError, OSError) as e:
            return f"error: {e}"
        _file_cache.pop(p.resolve(), None)
        _served_reads.pop(str(p.resolve()), None)
        return f"deleted {path}"

    def list_dir(path: str = ".") -> str:
        """List entries of a directory in the repository (dirs end '/')."""
        resolved_note = ""
        try:
            with trace_stage("list_dir"):
                try:
                    d = _safe(root, path, extra_roots=extra_roots)
                except ValueError:
                    # If the caller passed an absolute path that
                    # escapes, try interpreting it as relative —
                    # agents sometimes pass container-absolute
                    # paths (e.g. /workspace/...).  Only accept
                    # the fallback when the relative form actually
                    # exists on disk — otherwise the agent truly
                    # tried to escape.
                    if path.startswith("/"):
                        rel_tail = path[1:]
                        if rel_tail:
                            d = _safe(root, rel_tail, extra_roots=extra_roots)
                            if not d.exists():
                                raise
                            resolved_note = (
                                f"(resolved absolute {path!r} → relative {rel_tail!r}; "
                                f"use repo-relative paths)\n"
                            )
                        else:
                            raise
                    else:
                        raise
                if not d.exists():
                    # Path does not exist — try src/ fallback before
                    # letting iterdir() raise FileNotFoundError.  Same
                    # root-cause as the read_file fallback: agents probe
                    # e.g. "robotsix_llmio/core" when the package lives
                    # under src/robotsix_llmio/core/.
                    candidates = src_path_candidates(path)
                    for cand in candidates[1:]:
                        try:
                            alt = _safe(root, cand, extra_roots=extra_roots)
                        except ValueError, OSError:
                            continue
                        if alt.is_dir():
                            listing = "\n".join(
                                sorted(
                                    f"{e.name}/" if e.is_dir() else e.name
                                    for e in alt.iterdir()
                                )
                            )
                            return (
                                f"(resolved {path!r} → {cand!r}: "
                                f"package paths live under the src/ namespace)\n"
                                f"{listing}"
                            )
                    # No fallback found — return a graceful message
                    # instead of letting iterdir() raise FileNotFoundError,
                    # which wastes tokens and pollutes trace observability.
                    return (
                        f"error: {path!r} does not exist — "
                        f"try list_dir('.') to find the correct path"
                    )
                if d.is_file():
                    return (
                        f"error: '{path}' is a file, not a directory"
                        " — use read_file to read its content"
                    )
                return resolved_note + "\n".join(
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
        root. Use ``cd <subdir> && …`` to work in a subdirectory.

        Note: the ``pytest --timeout`` flag is not available in this
        environment — use plain ``python -m pytest`` without it.

        For fast symbol/string search across the repository before
        reading files, use ``git grep <pattern>`` (or plain ``grep``
        when ``.git`` is absent).  Prefer this over exhaustive
        ``read_file`` loops — it finds the files that matter in one
        command so you can then ``read_file`` only those."""
        if not root.exists():
            return (
                "error: workspace repo directory does not exist — "
                "the repository has not been cloned yet"
            )
        try:
            with trace_stage("run_command"):
                rc, out = sandbox.run(
                    command,
                    repo_dir=root,
                    settings=settings,
                    sandbox_image=sandbox_image,
                )
        except sandbox.DaemonUnavailableError as e:
            return f"daemon_unavailable: {e}"
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
