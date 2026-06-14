"""Shared leaf module for the implement stage package.

Pure leaf (Pattern A): holds every module-level name that more than one
implement submodule needs — constants, the stateless binary-artifact
helpers, the markdown-backtick regex, the internal dataclasses, and the
package ``log``. Imports only **outward** (``..base``, stdlib); it must
NOT import any sibling mixin or ``core`` so the package import graph
stays an acyclic DAG.

The ``log`` here is bound to the logger name
``"robotsix_mill.stages.implement"`` so existing
``caplog.at_level(logger="robotsix_mill.stages.implement")`` assertions
keep capturing through the package split.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..base import Outcome

log = logging.getLogger("robotsix_mill.stages.implement")

# Markdown-backtick extraction regex (compiled once as a module constant).
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# --- binary-artifact detection --------------------------------------------

BINARY_ARTIFACT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pyc",
        ".so",
        ".dylib",
        ".dll",
        ".o",
        ".a",
        ".bin",
        ".exe",
    }
)


# Number of out-of-scope file paths to show in the flood-guard
# operator note before truncating with a "+N more" marker — keeps the
# note readable when an artifact flood leaves hundreds of files.
_FLOOD_SAMPLE_SIZE = 20


def _is_binary_artifact(repo_dir: Path, path: str, target_branch: str) -> bool:
    """Return True if *path* is a binary artifact.

    Uses three orthogonal signals; any is sufficient:

    1. **Extension-based**: the path suffix matches a known binary
       extension (``.db``, ``.pyc``, ``.so``, …).
    2. **Git-based**: ``git diff --numstat origin/<target> -- <path>``
       returns ``-\t-\t<path>`` — the canonical binary marker.
    3. **Null-byte**: reads the first 8192 bytes of the file; a null
       byte identifies ELF, PE, Mach-O, PNG, JPG, and other binary
       formats regardless of extension.  This catches **untracked**
       files (which produce no ``git diff`` output).
    """
    # Extension-based check (fast path).
    suffix = Path(path).suffix.lower()
    if suffix in BINARY_ARTIFACT_EXTENSIONS:
        return True

    # Git-based check for misnamed binaries.
    try:
        numstat = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--numstat",
                f"origin/{target_branch}",
                "--",
                path,
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if numstat:
            parts = numstat.split("\t")
            if len(parts) >= 2 and parts[0] == "-" and parts[1] == "-":
                return True
    except subprocess.CalledProcessError:
        log.debug(
            "_is_binary_artifact: git numstat failed for %s — ignoring git failure",
            path,
            exc_info=True,
        )

    # Untracked-files check: files not yet tracked by git produce no
    # diff numstat output.  Read a small prefix and check for null bytes
    # — a standard heuristic that catches ELF, PE, Mach-O, PNG, JPG, etc.
    # regardless of file extension.
    try:
        file_path = repo_dir / path
        if file_path.is_file():
            with open(file_path, "rb") as f:
                head = f.read(8192)
            if b"\0" in head:
                return True
    except OSError:
        pass

    return False


# --- docs/modules.yaml re-path auto-detection ------------------------------


MODULES_YAML = "docs/modules.yaml"


def _modules_yaml_added_paths(repo_dir: Path, target_branch: str) -> set[str]:
    """Return the set of repo-relative path tokens ADDED to
    docs/modules.yaml relative to origin/<target_branch>.

    Parses the unified diff: for every added line (starts with '+'
    but not the '+++' header), strip the '+', surrounding
    whitespace, and an optional leading YAML list marker '- ';
    keep the remainder when it looks like a repo path (contains
    '/', no embedded whitespace, not a comment). These are the
    file paths the diff newly registers in the taxonomy.
    """
    try:
        raw = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                f"origin/{target_branch}",
                "--",
                MODULES_YAML,
            ],
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return set()

    paths: set[str] = set()
    for line in raw.split("\n"):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        # Strip the leading '+' and surrounding whitespace.
        token = line[1:].strip()
        # Strip an optional YAML list marker '- '.
        if token.startswith("- "):
            token = token[2:].strip()
        # Keep only tokens that look like repo paths:
        # contain '/' and no embedded whitespace, not a comment.
        if (
            "/" in token
            and not any(c.isspace() for c in token)
            and not token.startswith("#")
        ):
            paths.add(token)
    return paths


# ---------------------------------------------------------------------------
# Internal dataclasses for the refactored implement loop
# ---------------------------------------------------------------------------


@dataclass
class _ImplementContext:
    """Artifact bundle loaded once before the fix loop starts."""

    spec: str
    memory_text: str
    reference_files: list | None
    file_map: set[str] | None
    feedback: str | None
    previous_attempt_summary: str | None
    open_thread_ids: set[int] | None = None


@dataclass
class _ScopeGuardrailResult:
    """Returned by :meth:`_run_scope_guardrail`."""

    action: Literal["continue", "skip_iteration", "return"]
    outcome: Outcome | None = None
    file_map: set[str] | None = None
    feedback: str | None = None


@dataclass
class _SinglePassResult:
    """Returned by :meth:`_run_single_implement_pass`."""

    next_action: Literal["proceed", "retry", "escalate", "return", "pause", "skip"]
    outcome: Outcome | None = None
    feedback: str | None = None
    ic: _ImplementContext | None = None


@dataclass
class _AgentRunOutcome:
    """Result of the agent invocation phase.

    Exactly one of ``success`` / ``failure`` is non-None.  ``success``
    holds the 7-tuple returned by ``coding.run_implement_agent``
    (summary, ref_files, updated_memory, conv_state, new_msgs,
    no_change_needed, no_change_rationale); ``failure`` holds the
    ``_SinglePassResult`` the orchestrator should return when the agent
    call raised a caught error.  Used only inside ``implement.py`` to
    let the orchestrator early-return cleanly without leaking the
    dual-path complexity.
    """

    success: tuple | None = None
    failure: _SinglePassResult | None = None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------
