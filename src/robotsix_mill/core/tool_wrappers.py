"""Tool wrappers for pipeline safety guards.

These wrappers are applied at agent construction time (before tools are
passed to ``build_agent_from_definition``) and operate transparently —
the LLM sees the same tool signatures, but error returns are intercepted
to detect repetitive failures.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any


def _classify_error(error_str: str) -> str:
    """Classify an ``"error: ..."`` string into a stable, coarse kind."""
    if "does not exist" in error_str:
        return "not_exist"
    if "escapes the repository" in error_str:
        return "escapes_sandbox"
    if "is a directory, not a file" in error_str:
        return "is_directory"
    if "is a file, not a directory" in error_str:
        return "is_file"
    return "other"


def _make_consecutive_error_wrapper(
    fn: Callable[..., Any],
    state: dict[tuple[str, str], int],
    max_consecutive: int,
) -> Callable[..., Any]:
    """Return a wrapper around *fn* that tracks consecutive same-path errors.

    Args:
        fn: The original tool callable (``read_file`` or ``list_dir``).
        state: Shared ``(path, error_kind) → count`` dict across all
            wrapped tools in this guard invocation.
        max_consecutive: How many consecutive identical-path errors
            are tolerated before raising ``ModelRetry``.
    """
    from pydantic_ai.exceptions import ModelRetry

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)

        # Only inspect string results that look like errors.
        if not isinstance(result, str) or not result.startswith("error:"):
            state.clear()
            return result

        # pydantic-ai always passes tool arguments as keyword
        # arguments; both read_file and list_dir accept ``path``.
        path = kwargs.get("path", "?")
        error_kind = _classify_error(result)
        key = (str(path), error_kind)

        # A different (path, kind) pair means the agent has moved
        # on — reset the detector for the previous target.
        other_keys = [k for k in state if k != key]
        for k in other_keys:
            del state[k]

        state[key] = state.get(key, 0) + 1
        if state[key] >= max_consecutive:
            state.clear()
            raise ModelRetry(
                f"Path {path!r} does not exist — do NOT retry this "
                f"path. Move on to other work."
            )

        return result

    return wrapper


def wrap_read_tools_with_consecutive_error_guard(
    tools: list[Callable[..., Any]],
    max_consecutive: int = 3,
) -> list[Callable[..., Any]]:
    """Wrap ``read_file`` and ``list_dir`` tools with a consecutive-same-error detector.

    When the same path returns the same class of error *max_consecutive*
    times in a row (with no successful tool call in between), the wrapper
    raises :class:`pydantic_ai.exceptions.ModelRetry` with an instruction
    to abandon that path — instead of returning the raw ``"error: ..."``
    string for the Nth time.

    Only wraps tools whose ``__name__`` is ``"read_file"`` or
    ``"list_dir"``.  Other tools pass through unchanged.

    Args:
        tools: The list of tool callables to (selectively) wrap.
        max_consecutive: How many consecutive identical-path errors
            are tolerated before raising ``ModelRetry`` (default 3).

    Returns:
        A new list where ``read_file`` and ``list_dir`` are wrapped;
        all other entries are the original callables.
    """
    # Shared state: (path, error_kind) → consecutive count.
    # Cleared entirely whenever *any* wrapped tool returns a
    # non-error result, so a successful read_file/list_dir
    # anywhere resets the detector.
    state: dict[tuple[str, str], int] = {}

    READ_TOOL_NAMES = frozenset({"read_file", "list_dir"})

    wrapped: list[Callable[..., Any]] = []
    for t in tools:
        name = getattr(t, "__name__", "")
        if name in READ_TOOL_NAMES:
            wrapped.append(_make_consecutive_error_wrapper(t, state, max_consecutive))
        else:
            wrapped.append(t)
    return wrapped
