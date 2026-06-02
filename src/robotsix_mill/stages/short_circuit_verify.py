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
