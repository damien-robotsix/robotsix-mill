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
import re
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _parse_new_messages(
    new_messages: bytes | str | None,
    *,
    fail_open: bool = True,
    log_prefix: str = "",
) -> list[dict[str, Any]] | None:
    """Parse *new_messages* as a JSON list of message dicts.

    Returns the parsed list on success. On failure:

    - *fail_open=True* (default): logs a warning and returns ``[]`` —
      the caller should proceed as if no messages were present. Used by
      the ``run_*`` scanners.
    - *fail_open=False*: logs a warning and returns ``None`` —
      the caller should fail closed (BLOCK). Used by
      :func:`extract_replayable_edits`.

    Empty *new_messages* always returns ``[]`` regardless of *fail_open*.
    """
    if not new_messages:
        return []
    try:
        messages = json.loads(new_messages)
    except json.JSONDecodeError, TypeError, ValueError:
        log.warning(
            "%s: invalid messages JSON; %s",
            log_prefix,
            "assuming no edits" if fail_open else "failing closed",
        )
        return [] if fail_open else None
    if not isinstance(messages, list):
        return [] if fail_open else None
    return messages


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
        # Claude Agent SDK built-in editors (Claude SDK agents)
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
    messages = _parse_new_messages(
        new_messages, fail_open=True, log_prefix="run_invoked_edit_tools"
    )
    if not messages:
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
    scanner here.
    """
    messages = _parse_new_messages(
        new_messages, fail_open=True, log_prefix="run_claimed_edited_rawpaths"
    )
    if not messages:
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
    messages = _parse_new_messages(
        new_messages, fail_open=True, log_prefix="run_claimed_edited_paths"
    )
    if not messages:
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


def claimed_edits_already_on_branch(
    *,
    new_messages: bytes | str | None,
    branch_changed_files: list[str],
) -> bool:
    """True when every file the run claimed to edit is already changed on the
    branch — i.e. the edits were an idempotent re-application, not lost work.

    This disambiguates the one case where an edit-claim contradiction is a
    false alarm. On a *resuming* run whose branch is already ahead of target,
    prior passes have committed the implementation. A fresh pass re-reads the
    spec, finds the change present, and writes the same bytes back; git sees
    no diff, and the plain "edit tools ran but nothing changed" test reads that
    as work that failed to persist. It did persist — one pass earlier.

    Requiring *every* claimed path to already be part of the branch's diff
    keeps the guard's teeth. An agent that edited a file the branch never
    touched really did lose work, and still trips the contradiction.

    Matching is by basename, the same deterministic anchor
    :func:`run_claimed_edited_paths` uses, so absolute Claude SDK paths and
    repo-relative mill paths compare alike.

    Fails **closed**: no claimed paths, or no branch changes, returns ``False``
    so the caller keeps its existing blocking behaviour. This function may only
    ever excuse a contradiction on positive evidence.
    """
    claimed = run_claimed_edited_paths(new_messages)
    if not claimed:
        return False
    on_branch = {os.path.basename(p) for p in branch_changed_files if p}
    if not on_branch:
        return False
    return all(name in on_branch for name in claimed)


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


def extract_replayable_edits(
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
    messages = _parse_new_messages(
        new_messages, fail_open=False, log_prefix="extract_replayable_edits"
    )
    if messages is None:
        return None
    if not messages:
        return []
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
    same non-progress tool >= *same_tool_window* long, else ``None``.
    """
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
    tool calls at the end of *tool_names*.
    """
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


# Commit-SHA-like token (7-40 hex chars). Deliberately identical to the refine
# stage's ``_COMMIT_SHA_RE`` so both stages recognise the same citations.
_COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def cited_fix_unverified(repo_dir: Path | str | None, text: str | None) -> str | None:
    """Return a diagnostic when a closure rationale claims an already-shipped
    fix citing a commit that is NOT present at ``origin/main``, else ``None``.

    The empty-diff → DONE short-circuit trusts the agent's "already fixed
    elsewhere" rationale. When that rationale cites a commit SHA, verify it
    against the live tree before closing: the commit must be a real object AND
    an ancestor of ``origin/main`` (``git cat-file -t`` reports ``commit`` and
    ``git merge-base --is-ancestor <sha> origin/main`` exits 0). A cited SHA
    that is missing or not an ancestor is an unverified merge/completion claim
    — the work was never actually merged, so the caller MUST route the ticket
    back for the real fix instead of closing it DONE.

    Only fires when the rationale actually claims an external fix (reusing the
    refine stage's ``_rationale_claims_external_fix`` detector) so a stray
    hex-like token in a legitimate no-change rationale never triggers a false
    block, and only when at least one cited SHA fails verification. Any git
    error is treated as "not verified" so a transient failure never lets a
    false close through. Returns ``None`` when there is no external-fix claim,
    no cited SHA, or every cited SHA is verified.
    """
    # Lazy import: keeps the heavy refine module out of the implement-stage
    # import path and avoids any theoretical cross-stage import cycle.
    from .refine.helpers import _rationale_claims_external_fix

    if repo_dir is None:
        return None
    rationale = text or ""
    if not _rationale_claims_external_fix(rationale):
        return None
    shas = list(dict.fromkeys(_COMMIT_SHA_RE.findall(rationale.lower())))
    if not shas:
        return None

    unverified: list[str] = []
    for sha in shas:
        try:
            type_check = subprocess.run(
                ["git", "-C", str(repo_dir), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
            )
            if type_check.returncode != 0 or type_check.stdout.strip() != "commit":
                unverified.append(sha)
                continue
            anc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "merge-base",
                    "--is-ancestor",
                    sha,
                    "origin/main",
                ],
                capture_output=True,
                text=True,
            )
            if anc.returncode != 0:
                unverified.append(sha)
        except Exception:
            log.warning(
                "cited-fix verification failed for %s — treating as unverified",
                sha,
                exc_info=True,
            )
            unverified.append(sha)

    if not unverified:
        return None
    return (
        "closure rationale claims an already-shipped fix citing commit(s) "
        + ", ".join(unverified)
        + " that are NOT present at origin/main (not a valid commit object, or "
        "not an ancestor of origin/main). This is an unverified "
        "merge/completion claim — the work was not actually done. Routing back "
        "for the real fix instead of closing as done."
    )
