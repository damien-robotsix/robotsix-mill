"""Guards for the empty-diff → DONE short-circuit.

A ticket whose implement run produces an empty working-tree diff is normally
closed as *no change needed*: the agent judged the spec already satisfied, so
there is nothing to ship. But an empty diff can ALSO mean the run *attempted*
edits that never persisted — the agent called file-mutating tools (and claimed
success in its summary) yet the working tree still matches the target branch
because the edits were reverted, the workspace was reset mid-run, or the writes
landed outside the clone. Routing that case to DONE silently loses the work and
falsely completes the ticket.

This is not hypothetical: ticket 904a — whose entire purpose was to add this
guard — was itself closed this way. Its implement summary described new files
and "341 tests pass", but the committed branch matched main exactly, so the
empty-diff→DONE path fired and the work vanished.

:func:`detect_edit_claim_contradiction` distinguishes the two readings of an
empty diff by scanning the run's *new* messages for invocations of file-mutating
tools. Pure command-runner tools (``run_command`` / ``Bash``) are deliberately
excluded: a genuine no-change run routinely runs tests or greps without editing,
and counting those as an edit claim would block legitimate no-change closes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Tools whose invocation asserts a file mutation. Command-runner tools
# (``run_command`` / ``Bash``) are intentionally absent — they read as often as
# they write, so their mere presence is not a reliable edit claim and would
# produce false contradictions on legitimate no-change runs.
_EDIT_TOOL_NAMES = frozenset(
    {
        # mill rooted filesystem tools (agents/fs_tools.py, spawn_subtask)
        "write_file",
        "edit_file",
        "delete_file",
        # Claude Agent SDK built-in editors (level-3 / Claude SDK agents)
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
    }
)


def run_invoked_edit_tools(new_messages: bytes | str | None) -> list[str]:
    """Return the names of file-mutating tools invoked in *new_messages*.

    *new_messages* MUST be the ``new_messages_json()`` payload from the agent
    run — the messages added during THIS run only. Passing the full
    ``all_messages_json()`` would re-count a prior run's edit calls after a
    resume and manufacture a false contradiction (the same trap documented in
    :func:`robotsix_mill.stages.pause.check_for_pause`).

    Malformed or empty input yields ``[]`` (fail-open: never invent a
    contradiction from a parse error — that would wrongly BLOCK good runs).
    """
    if not new_messages:
        return []
    try:
        messages = json.loads(new_messages)
    except json.JSONDecodeError, TypeError, ValueError:
        log.warning("run_invoked_edit_tools: invalid messages JSON; assuming no edits")
        return []
    if not isinstance(messages, list):
        return []
    found: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("part_kind") != "tool-call":
                continue
            name = part.get("tool_name")
            if name in _EDIT_TOOL_NAMES:
                found.append(name)
    return found


def _claimed_rawpath_from_part(part: object) -> str | None:
    """Return the edit-target path of a tool-call *part* verbatim, else
    ``None``.

    Encapsulates the per-part filtering and path extraction so the path
    scanners stay flat scans. A part qualifies only when it is an
    ``_EDIT_TOOL_NAMES`` tool-call carrying a non-empty string path under
    ``args["path"]`` (mill fs tools) or ``args["file_path"]`` (Claude SDK
    editors). Anything else fails open to ``None``.
    """
    if not isinstance(part, dict):
        return None
    if part.get("part_kind") != "tool-call":
        return None
    if part.get("tool_name") not in _EDIT_TOOL_NAMES:
        return None
    args = part.get("args")
    if not isinstance(args, dict):
        return None
    # mill fs tools key the target as ``path``; the Claude SDK editors key it
    # as ``file_path``. Prefer ``path`` when present.
    raw_path = args.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raw_path = args.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    return raw_path


def _claimed_path_from_part(part: object) -> str | None:
    """Return the edit-target basename of a tool-call *part*, else ``None``."""
    raw_path = _claimed_rawpath_from_part(part)
    if raw_path is None:
        return None
    return os.path.basename(raw_path) or None


def run_claimed_edited_rawpaths(new_messages: bytes | str | None) -> list[str]:
    """Return the de-duplicated VERBATIM paths edit tool-calls targeted.

    Same scan as :func:`run_claimed_edited_paths` but keeps the full path
    exactly as the tool-call carried it (repo-relative for mill fs tools,
    absolute for the Claude SDK editors) instead of reducing to a basename.
    Used by the gitignored-edit detector, which needs the real location to
    ask ``git check-ignore``. Fail-open on malformed input, like every
    scanner here."""
    if not new_messages:
        return []
    try:
        messages = json.loads(new_messages)
    except json.JSONDecodeError, TypeError, ValueError:
        log.warning(
            "run_claimed_edited_rawpaths: invalid messages JSON; assuming no edits"
        )
        return []
    if not isinstance(messages, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []) or []:
            raw = _claimed_rawpath_from_part(part)
            if raw and raw not in seen:
                seen.add(raw)
                found.append(raw)
    return found


def run_claimed_edited_paths(new_messages: bytes | str | None) -> list[str]:
    """Return the de-duplicated basenames of files an edit tool-call targeted.

    Reuses the same message-scanning skeleton as
    :func:`run_invoked_edit_tools`, but instead of the tool *names* it
    extracts the target file *path* from each edit tool-call's ``args`` and
    returns the path *basenames* (e.g. ``board.js``). The path is read from
    ``args["path"]`` (mill fs tools ``write_file`` / ``edit_file`` /
    ``delete_file`` — see ``agents/fs_tools.py``) when present, else from
    ``args["file_path"]`` (Claude SDK ``Write`` / ``Edit`` / ``MultiEdit`` /
    ``NotebookEdit``).

    Basename-level matching is the deterministic v1 anchor: it is robust to
    the absolute (Claude SDK) vs repo-relative (mill) path mismatch without
    needing the repo root, at the accepted cost of confusing two like-named
    files in different directories.

    Malformed or empty input — invalid JSON, non-list payload, missing
    ``args``, or missing/non-string path keys — fails open exactly like
    :func:`run_invoked_edit_tools`: the offending entry is skipped (or ``[]``
    is returned). A parse error must never manufacture a contradiction.
    """
    if not new_messages:
        return []
    try:
        messages = json.loads(new_messages)
    except json.JSONDecodeError, TypeError, ValueError:
        log.warning(
            "run_claimed_edited_paths: invalid messages JSON; assuming no edits"
        )
        return []
    if not isinstance(messages, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []) or []:
            base = _claimed_path_from_part(part)
            if base and base not in seen:
                seen.add(base)
                found.append(base)
    return found


def detect_missing_claimed_files(
    *,
    changed_files: list[str],
    new_messages: bytes | str | None,
    summary: str | None,
) -> list[str]:
    """Basenames the run *claims* it edited but that are absent from the diff.

    This is the per-file generalization of
    :func:`detect_edit_claim_contradiction`: instead of firing only when the
    *whole* net diff is empty, it catches the non-empty-diff case where the
    bulk of the work landed but a few specifically-named sub-fixes never
    reached disk (the implement summary / thread-reply asserts edits the diff
    does not contain). Anchored deterministically on file paths only — no NL
    parsing of symbol- or line-level claims.

    A basename is reported as *missing* only when ALL of:

    1. it was targeted by an ``_EDIT_TOOL_NAMES`` tool-call this run
       (per :func:`run_claimed_edited_paths`), AND
    2. it appears as a substring of *summary* (case-sensitive), AND
    3. it is NOT among the net-diff ``changed_files`` basenames.

    The *summary* gate is a required false-positive guard: a file the agent
    edited and then reverted (a legitimate net-zero, e.g. via ``git
    checkout``) is targeted by an edit tool-call but is NOT named as a landed
    fix in the summary, so it must not be flagged. Requiring presence in
    *summary* filters that edit-then-revert case while staying deterministic.
    When *summary* is falsy nothing is claimed → ``[]``.

    Returns the sorted basenames that are claimed-but-missing. An empty result
    means the run is consistent (safe to proceed). Fail-open: any parse error
    upstream yields ``[]`` (never invents a contradiction from bad input).
    """
    if not summary:
        return []
    claimed = set(run_claimed_edited_paths(new_messages))
    # Restrict to basenames the summary text actually names as edited —
    # filters the edit-then-revert false positive (see docstring).
    claimed = {base for base in claimed if base in summary}
    landed = {os.path.basename(f) for f in changed_files}
    return sorted(claimed - landed)


# Edit tools whose effect can be faithfully replayed from the recorded
# tool-call args alone (path + old/new text, or full content, or a delete).
# ``MultiEdit`` / ``NotebookEdit`` carry structured multi-step payloads that
# are not safe to reconstruct, so a run that used them is treated as
# un-replayable (the caller fails closed → BLOCK).
_REPLAYABLE_EDIT_TOOLS = frozenset(
    {"write_file", "edit_file", "delete_file", "Write", "Edit"}
)


def _part_args(part: dict[str, object]) -> dict[str, object] | None:
    """Return a tool-call part's ``args`` as a dict, or ``None``.

    pydantic-ai persists ``args`` either as a dict or as a JSON-encoded
    string; both are accepted. Anything else fails open to ``None``.
    """
    args = part.get("args")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError, TypeError, ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def extract_replayable_edits(  # noqa: C901 — flat per-tool arg dispatch; branch count is inherent
    new_messages: bytes | str | None,
) -> list[dict[str, str]] | None:
    """Return the run's edit tool-calls as replayable ops, or ``None``.

    Each op is a dict ``{"kind", "path", ...}`` where *kind* is one of
    ``"edit"`` (``old`` + ``new`` text), ``"write"`` (full ``content``), or
    ``"delete"``. *path* is the verbatim tool-call path (repo-relative for
    mill fs tools, absolute for the Claude SDK editors).

    Returns ``None`` (a *can't-replay-safely* signal the caller MUST treat as
    BLOCK) when the run invoked an edit tool that cannot be faithfully
    replayed — an un-replayable kind (``MultiEdit`` / ``NotebookEdit``) or a
    call missing the args needed to reproduce it. This keeps the work-loss
    guard fully intact whenever the formatter-revert check is inapplicable.

    Returns ``[]`` when no edit tool was invoked at all (no contradiction to
    resolve). Fail-closed: malformed top-level JSON yields ``None``.
    """
    if not new_messages:
        return []
    try:
        messages = json.loads(new_messages)
    except json.JSONDecodeError, TypeError, ValueError:
        log.warning("extract_replayable_edits: invalid messages JSON; failing closed")
        return None
    if not isinstance(messages, list):
        return None
    ops: list[dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("part_kind") != "tool-call":
                continue
            name = part.get("tool_name")
            if name not in _EDIT_TOOL_NAMES:
                continue
            if name not in _REPLAYABLE_EDIT_TOOLS:
                return None  # un-replayable edit kind → fail closed
            args = _part_args(part)
            if args is None:
                return None
            path = args.get("path")
            if not isinstance(path, str) or not path:
                path = args.get("file_path")
            if not isinstance(path, str) or not path:
                return None
            if name == "delete_file":
                ops.append({"kind": "delete", "path": path})
            elif name in ("write_file", "Write"):
                content = args.get("content")
                if not isinstance(content, str):
                    return None
                ops.append({"kind": "write", "path": path, "content": content})
            else:  # edit_file / Edit
                old = args.get("old_string")
                new = args.get("new_string")
                if not isinstance(old, str) or not isinstance(new, str):
                    return None
                ops.append({"kind": "edit", "path": path, "old": old, "new": new})
    return ops


# --- stuck-loop detection ---------------------------------------------------

# Tools whose repeated invocation without any file edits or test runs
# signals a stuck agent (e.g. reading the ticket or listing epic children
# in a loop).  ``run_command`` is absent because it is the primary test-
# running tool — an agent that runs tests is at least verifying something.
_NON_PROGRESS_TOOLS = frozenset(
    {
        "read_ticket",
        "list_epic_children",
        "list_threads",
        "read_file",
        "list_dir",
        "explore",
        "parallel_explore",
        "consult_expert",
        "ask_web_knowledge",
    }
)

# Tools whose presence in a pass counts as "making progress" even if no
# file diff results — e.g. the agent ran tests, posted a comment, or
# paused to ask a question.  A pass with ONLY these + non-progress tools
# is still stuck if it produced no diff, but they keep the "same tool
# repeat" detector from firing spuriously when the agent is actually
# testing / communicating.
_PROGRESS_SIGNAL_TOOLS = frozenset(
    {
        "run_command",
        "spawn_subtask",
        "post_comment",
        "insert_changelog_entry",
        "ask_user",
        "reply_to_thread",
    }
)


def _extract_tool_names(messages: list[Any]) -> list[str]:
    """Return the ordered tool-call names from a pydantic-ai message list."""
    names: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("part_kind") != "tool-call":
                continue
            name = part.get("tool_name")
            if isinstance(name, str):
                names.append(name)
    return names


def _detect_stuck_same_tool(
    tool_names: list[str],
    *,
    same_tool_window: int,
) -> str | None:
    """Return the tool name if the tail of *tool_names* is a run of the
    same non-progress tool >= *same_tool_window* long, else ``None``."""
    total = len(tool_names)
    if total < same_tool_window:
        return None
    tail_name = tool_names[-1]
    if tail_name not in _NON_PROGRESS_TOOLS:
        return None
    run_len = 0
    for i in range(total - 1, -1, -1):
        if tool_names[i] == tail_name:
            run_len += 1
        else:
            break
    return tail_name if run_len >= same_tool_window else None


def _trailing_non_progress_run(tool_names: list[str]) -> int:
    """Return the length of the trailing run of consecutive non-progress
    tool calls at the end of *tool_names*."""
    run = 0
    for i in range(len(tool_names) - 1, -1, -1):
        if tool_names[i] in _NON_PROGRESS_TOOLS:
            run += 1
        else:
            break
    return run


def analyze_pass_progress(
    new_messages: bytes | str | None,
    *,
    same_tool_window: int = 5,
) -> dict[str, Any]:
    """Analyze *new_messages* for stuck-loop signals.

    Returns a dict with:
    - ``total``: total tool-call count in this pass
    - ``edit_calls``: number of file-mutating tool calls
    - ``progress_calls``: number of progress-signal tool calls
    - ``stuck_same_tool``: name of the tool that was called *same_tool_window*
      consecutive times as the most recent non-progress calls, or ``None``
    - ``last_non_progress_run``: length of the trailing run of consecutive
      non-progress tool calls

    Malformed / empty input returns zeros / None.
    """
    empty = {
        "total": 0,
        "edit_calls": 0,
        "progress_calls": 0,
        "stuck_same_tool": None,
        "last_non_progress_run": 0,
    }
    if not new_messages:
        return empty
    try:
        messages = json.loads(new_messages)
    except json.JSONDecodeError, TypeError, ValueError:
        log.warning("analyze_pass_progress: invalid messages JSON; assuming empty")
        return empty
    if not isinstance(messages, list):
        return empty

    tool_names = _extract_tool_names(messages)

    total = len(tool_names)
    edit_calls = sum(1 for n in tool_names if n in _EDIT_TOOL_NAMES)
    progress_calls = sum(1 for n in tool_names if n in _PROGRESS_SIGNAL_TOOLS)

    return {
        "total": total,
        "edit_calls": edit_calls,
        "progress_calls": progress_calls,
        "stuck_same_tool": _detect_stuck_same_tool(
            tool_names, same_tool_window=same_tool_window
        ),
        "last_non_progress_run": _trailing_non_progress_run(tool_names),
    }


def detect_edit_claim_contradiction(
    *, has_changes: bool, new_messages: bytes | str | None
) -> list[str]:
    """Names of edit tools the run invoked despite producing no diff.

    A non-empty result is an *edit-claim contradiction*: the run mutated files
    (per its own tool calls) but the working tree matches the target branch, so
    the work did not persist. The empty-diff→DONE short-circuit MUST be skipped
    in that case — the caller should BLOCK for inspection instead.

    An empty result means the empty diff is consistent with a genuine no-change
    run (the agent only read / ran commands, or made no tool calls at all), and
    the short-circuit is safe to take.

    When *has_changes* is True there is a real diff, so no short-circuit is
    happening and there is nothing to verify — returns ``[]``.
    """
    if has_changes:
        return []
    return sorted(set(run_invoked_edit_tools(new_messages)))
