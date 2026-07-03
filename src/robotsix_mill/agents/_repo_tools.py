"""Shared helper: build repo-scoped tool list for an agent.

Extracted to eliminate copy-pasted tool-construction blocks across
answering, module_curator, bespoke,
config_syncing, trace_inspector, and refining agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings


def _build_repo_tools(
    repo_dir: Path | None,
    settings: Settings,
    *,
    tool_names: tuple[str, ...] = ("read_file", "list_dir", "run_command"),
    extra_roots: list[Path] | None = None,
    include_parallel_explore: bool = False,
    include_explore: bool = True,
    read_file_max_calls: int | None = None,
) -> list[Any]:
    """Return repo-scoped tools when *repo_dir* is set, else [].

    Builds a list of exploration + read-only filesystem tools that
    ground an agent in a local repository clone.  When *repo_dir* is
    ``None`` returns an empty list so the agent runs in reasoning-only
    mode (no repo access).

    *include_explore* can be set to ``False`` to suppress the ``explore``
    tool (and *include_parallel_explore* must also be ``False`` for
    ``parallel_explore`` to be suppressed).  Both default to their
    current values so existing callers are unchanged.
    """
    if repo_dir is None:
        return []

    from .explore import make_explore_tool, make_parallel_explore_tool
    from .fs_tools import build_fs_tools

    ro = [
        t
        for t in build_fs_tools(
            repo_dir,
            settings,
            extra_roots=extra_roots,
            read_file_max_calls=read_file_max_calls,
        )
        if t.__name__ in tool_names
    ]
    explore_kwargs: dict[str, Any] = {"extra_roots": extra_roots} if extra_roots else {}
    tools: list[Any] = []
    if include_explore:
        tools.append(make_explore_tool(settings, repo_dir, **explore_kwargs))
    if include_parallel_explore:
        tools.insert(
            0, make_parallel_explore_tool(settings, repo_dir, **explore_kwargs)
        )
    tools.extend(ro)
    return tools
