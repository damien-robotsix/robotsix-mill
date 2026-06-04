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
        # Claude Agent SDK built-in editors (llm_backend=claude_sdk)
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


def _claimed_path_from_part(part: object) -> str | None:
    """Return the edit-target basename of a tool-call *part*, else ``None``.

    Encapsulates the per-part filtering and path extraction so
    :func:`run_claimed_edited_paths` stays a flat scan. A part qualifies only
    when it is an ``_EDIT_TOOL_NAMES`` tool-call carrying a non-empty string
    path under ``args["path"]`` (mill fs tools) or ``args["file_path"]``
    (Claude SDK editors). Anything else fails open to ``None``.
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
    return os.path.basename(raw_path) or None


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
