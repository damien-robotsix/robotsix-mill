"""Detect CI gates introduced by the ticket's own diff.

When a ticket adds a new CI gate (lint / format / analyzer step) and that
gate fails on pre-existing debt, the ci-fix agent tends to report
OUT_OF_SCOPE and spawn a ``ci_fix_dependency`` ticket. Fixing that debt is
part of the ticket — the gate cannot merge without it. These helpers
deterministically spot the case: the failing step's ``run:`` command (the
``##[group]Run <cmd>`` line before an ``##[error]`` in the job log) appears
in the lines the ticket's diff adds under ``.github/workflows/``.

No LLM; any git error degrades to "not introduced" (normal routing).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from ..scm.vcs import git_ops

log = logging.getLogger("robotsix_mill.ci_fix_gate")

_WORKFLOWS_DIR = ".github/workflows"
_LOG_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\S+Z ?")
_RUN_GROUP = "##[group]Run "
_ERROR = "##[error]"
# A containment match on a very short command ("make", "ls") is noise.
_MIN_CONTAINED_LEN = 8
# ``flutter-version: 3.47.2``, ``python-version: "3.14"``, ``node-version: 22``
_VERSION_PIN_RE = re.compile(
    r"^\s*(?:-\s*)?([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*-version)\s*:\s*(\S.*?)\s*$"
)
_MAX_PINS = 20


def _normalize(text: str) -> str:
    """Collapse whitespace and strip surrounding quotes."""
    return " ".join(text.split()).strip("'\"").strip()


def failing_run_commands(log_text: str) -> list[str]:
    """Return the ``run:`` command of each failing step in *log_text*.

    The failing step of a job is the last ``##[group]Run <cmd>`` line before
    an ``##[error]`` line. Only the first line of a multi-line script is
    logged in the group header, which is what we return.
    """
    commands: list[str] = []
    current: str | None = None
    for raw in log_text.splitlines():
        line = _LOG_TS_RE.sub("", raw.strip()).strip()
        if line.startswith(_RUN_GROUP):
            current = _normalize(line[len(_RUN_GROUP) :])
        elif line.startswith(_ERROR) and current:
            if current not in commands:
                commands.append(current)
            current = None
    return commands


def workflow_added_lines(repo_dir: str | Path, target_branch: str) -> list[str]:
    """Return the lines the branch adds to ``.github/workflows/**``.

    Uses ``git diff origin/<target>...HEAD`` (merge-base form, so commits
    landed on the target since the branch point are not counted). Returns
    ``[]`` on any git error.
    """
    try:
        diff = git_ops._git(
            Path(repo_dir),
            "diff",
            "--no-color",
            f"origin/{target_branch}...HEAD",
            "--",
            _WORKFLOWS_DIR,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("ci_fix_gate: workflow diff failed: %s", e)
        return []
    return [
        ln[1:]
        for ln in diff.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    ]


def _added_command(line: str) -> str:
    """Strip YAML list / ``run:`` prefixes off an added workflow line."""
    text = line.strip()
    if text.startswith("- "):
        text = text[2:].lstrip()
    if text.startswith("run:"):
        text = text[len("run:") :].lstrip()
    return _normalize(text)


def command_added_by_diff(command: str, added_lines: list[str]) -> bool:
    """True when *command* appears in one of *added_lines*."""
    cmd = _normalize(command)
    if not cmd:
        return False
    for line in added_lines:
        added = _added_command(line)
        if not added:
            continue
        if added == cmd:
            return True
        if len(cmd) >= _MIN_CONTAINED_LEN and cmd in added:
            return True
    return False


def self_introduced_gate_commands(
    failing_summary: str, repo_dir: str | Path, target_branch: str
) -> list[str]:
    """Return the failing step commands this branch's diff introduced.

    Empty when no failing ``run:`` command can be read from the log or none
    of them appears in the lines the diff adds to ``.github/workflows/**``.
    """
    commands = failing_run_commands(failing_summary)
    if not commands:
        return []
    added = workflow_added_lines(repo_dir, target_branch)
    if not added:
        return []
    return [c for c in commands if command_added_by_diff(c, added)]


def self_introduced_gate_hint(commands: list[str]) -> str:
    """Markdown block telling the agent the failing gate is in scope."""
    listed = "\n".join(f"- `{c}`" for c in commands)
    return (
        "## Failing gate introduced by THIS ticket\n"
        "The failing step(s) below were ADDED by this ticket's own diff to "
        "`.github/workflows/`. Pre-existing debt the new gate reports is IN "
        "SCOPE: fix it on this branch (e.g. run the formatter/linter with the "
        "same toolchain version CI pins) and do NOT report OUT_OF_SCOPE.\n"
        f"{listed}\n"
    )


def _workflow_texts(repo_dir: str | Path) -> list[tuple[str, str]]:
    """Return ``(relative path, text)`` for every workflow file."""
    wf_dir = Path(repo_dir) / _WORKFLOWS_DIR
    if not wf_dir.is_dir():
        return []
    texts: list[tuple[str, str]] = []
    for f in sorted([*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")]):
        try:
            texts.append((f"{_WORKFLOWS_DIR}/{f.name}", f.read_text()))
        except OSError:
            continue
    return texts


def _scoped_workflow_texts(
    texts: list[tuple[str, str]], commands: list[str] | None
) -> list[tuple[str, str]]:
    """Keep the workflows containing one of *commands* (all when none do)."""
    if not commands:
        return texts
    matching = [
        (name, text)
        for name, text in texts
        if any(c and c in " ".join(text.split()) for c in commands)
    ]
    return matching or texts


def workflow_toolchain_pins(
    repo_dir: str | Path, commands: list[str] | None = None
) -> list[str]:
    """Return ``<file>: <key>: <value>`` toolchain pins from the workflows.

    Scans ``.github/workflows/*.yml``/``*.yaml`` for ``*-version:`` keys
    (``flutter-version``, ``python-version``, …). When *commands* is given,
    only workflows whose text contains one of them are scanned (falls back to
    every workflow when none match).
    """
    texts = _scoped_workflow_texts(_workflow_texts(repo_dir), commands)
    pins: list[str] = []
    for name, text in texts:
        for line in text.splitlines():
            m = _VERSION_PIN_RE.match(line)
            if not m:
                continue
            pin = f"{name}: {m.group(1)}: {m.group(2)}"
            if pin not in pins:
                pins.append(pin)
            if len(pins) >= _MAX_PINS:
                return pins
    return pins
