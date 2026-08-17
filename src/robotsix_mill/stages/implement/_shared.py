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
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..base import Outcome

if TYPE_CHECKING:
    from ...config import Settings

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

# Minimum number of distinct marker entries (``*.dist-info``,
# ``*.egg-info``, ``node_modules``) required to classify a repo-root
# directory as a vendored-dep install target — unless ``node_modules``
# is present alone, which is always a strong marker (npm convention).
_VENDORED_DEP_MIN_MARKERS = 2


def _is_binary_artifact(repo_dir: Path, path: str, target_branch: str) -> bool:
    r"""Return True if *path* is a binary artifact.

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


# --- vendored-dep install-directory detection -----------------------------


def _vendored_dep_roots(
    repo_dir: Path,
    paths: list[str],
    target_branch: str,
) -> set[str]:
    """Return the set of repo-root directory names among *paths* that
    look like pip/uv/npm vendored-dependency install targets by CONTENT
    SIGNATURE (regardless of the dir's name) AND are NOT git-tracked.

    Every file under a returned root should be excluded from scope.
    """
    # 1. Group paths by first path component (repo-root directory).
    dir_files: dict[str, list[str]] = {}
    for p in paths:
        if "/" not in p:
            continue  # top-level files are never vendored roots
        root = p.split("/", 1)[0]
        dir_files.setdefault(root, []).append(p)

    vendored: set[str] = set()

    for root, member_paths in dir_files.items():
        # 2. Count distinct marker entries among path components.
        distinct_dist_info: set[str] = set()
        distinct_egg_info: set[str] = set()
        has_node_modules = False

        for p in member_paths:
            parts = p.split("/")
            for part in parts:
                if part == "node_modules":
                    has_node_modules = True
                elif part.endswith(".dist-info"):
                    distinct_dist_info.add(part)
                elif part.endswith(".egg-info"):
                    distinct_egg_info.add(part)

        marker_count = len(distinct_dist_info) + len(distinct_egg_info)
        if has_node_modules:
            marker_count += 1

        # 3. Classify: either node_modules is present (strong marker)
        #    or the distinct-marker count meets the threshold.
        if not has_node_modules and marker_count < _VENDORED_DEP_MIN_MARKERS:
            continue

        # 4. Tracked-ness gate: only auto-ignore if NO tracked files
        #    under this root directory. Fail-closed: any git error →
        #    treat as tracked (do not exclude).
        try:
            ls = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "ls-files",
                    "--",
                    root,
                ],
                capture_output=True,
                text=True,
            )
            if ls.returncode != 0:
                log.debug(
                    "_vendored_dep_roots: git ls-files failed for %s — "
                    "treating as tracked (fail-closed)",
                    root,
                )
                continue
            if ls.stdout.strip():
                # At least one tracked file → this dir is real source,
                # not a vendored-dep install target.
                log.debug(
                    "_vendored_dep_roots: %s has tracked files — "
                    "skipping (real source dir)",
                    root,
                )
                continue
        except subprocess.CalledProcessError:
            log.debug(
                "_vendored_dep_roots: git ls-files error for %s — "
                "treating as tracked (fail-closed)",
                root,
                exc_info=True,
            )
            continue

        vendored.add(root)

    return vendored


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
# Config-only change detection (deterministic skip candidate check)
# ---------------------------------------------------------------------------

CONFIG_ONLY_EXTENSIONS = (
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".cfg",
    ".ini",
    ".json",
    ".conf",
)


def _is_rename_only_change(repo_dir: Path, target_branch: str) -> bool:
    """True when the diff consists of file renames plus config/doc stubs.

    Returns ``True`` when there is at least one git rename AND every
    non-rename change (Added, Copied, Modified) is either a config/doc
    file matching :data:`CONFIG_ONLY_EXTENSIONS` or a zero-delta stub
    file (0 lines added, 0 lines removed).

    Also checks the working tree diff so that unstaged edits from a
    prior retry pass are detected before the author commits them.

    Fail-closed: returns ``False`` on any git error, when there are no
    renames, or when any non-rename change carries a real behavioural
    delta.
    """
    # -- Renames ----------------------------------------------------------
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--diff-filter=R",
            "--name-only",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    renames: list[str] = [p for p in result.stdout.strip().splitlines() if p]
    if not renames:
        return False  # No renames at all → not a rename-only change

    # -- Non-rename changes (Added, Copied, Modified) --------------------
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--diff-filter=ACM",
            "--name-only",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    non_renames: list[str] = [p for p in result.stdout.strip().splitlines() if p]

    # Working tree: catches edits from a prior retry pass (unstaged).
    wt = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--name-only"],
        capture_output=True,
        text=True,
    )
    if wt.returncode == 0 and wt.stdout.strip():
        non_renames.extend(wt.stdout.strip().splitlines())

    for p in non_renames:
        # Config-only extension → allowed.
        if p.lower().endswith(CONFIG_ONLY_EXTENSIONS):
            continue
        # Check whether the file is a zero-delta stub.
        numstat = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--numstat",
                f"origin/{target_branch}",
                "--",
                p,
            ],
            capture_output=True,
            text=True,
        )
        if numstat.returncode != 0:
            return False
        parts = numstat.stdout.strip().split("\t")
        if len(parts) < 2 or parts[0] != "0" or parts[1] != "0":
            return False  # Non-zero delta → real behavioural change

    return True


def _is_config_only_change(repo_dir: Path, target_branch: str) -> bool:
    """True when every changed file (added, copied, modified, renamed)
    relative to origin/<target_branch> has a config-only extension.

    Also checks the working tree diff so that unstaged edits from a prior
    retry pass are detected before the author commits them.

    Fail-closed: returns False on any git error or when there is no diff
    yet, so the full test gate runs as the safe default.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    changed: list[str] = result.stdout.strip().splitlines()

    # Working tree: catches edits from a prior retry pass (unstaged).
    wt = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--name-only"],
        capture_output=True,
        text=True,
    )
    if wt.returncode == 0 and wt.stdout.strip():
        changed.extend(wt.stdout.strip().splitlines())

    if not changed:
        return False  # no diff yet — run tests
    return all(p.lower().endswith(CONFIG_ONLY_EXTENSIONS) for p in changed)


def _is_small_mechanical_refactor(repo_dir: Path, target_branch: str) -> bool:
    """True when the diff against origin/<target_branch> is a small,
    fully-prescribed mechanical refactor — only modifications to existing
    files, ≤40 total lines changed (insertions + deletions).

    Fail-closed: returns False on any git error, empty diff, or when
    new files were added.
    """
    # 1. Check for new files (--diff-filter=A lists Added files).
    added = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--name-only",
            "--diff-filter=A",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        return False
    if added.stdout.strip():
        return False  # new files introduced → not a pure refactor

    # 2. Check total diff size via --shortstat.
    stat = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--shortstat",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if stat.returncode != 0:
        return False
    shortstat = stat.stdout.strip()
    if not shortstat:
        return False  # no diff → fail-closed

    # Parse "X files changed, Y insertions(+), Z deletions(-)" or
    # "X files changed, Y insertions(+)" (no deletions case).
    total = 0
    import re as _re

    ins = _re.search(r"(\d+)\s+insertions?\(\+\)", shortstat)
    if ins:
        total += int(ins.group(1))
    dels = _re.search(r"(\d+)\s+deletions?\(-\)", shortstat)
    if dels:
        total += int(dels.group(1))

    if total == 0:
        return False  # no real changes

    return total <= 40


# ---------------------------------------------------------------------------
# Spec-exact code-edit detection (deterministic implement bypass)
# ---------------------------------------------------------------------------

# Regex to find fenced code blocks in markdown.
# Group 1: optional info string (language, etc.)
# Group 2: code content
_FENCED_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

# Common source/test file extensions we expect in code blocks.
_SOURCE_EXTENSIONS = frozenset(
    {".py", ".js", ".ts", ".css", ".html", ".yaml", ".yml", ".toml", ".json", ".md"}
)


def _parse_spec_code_blocks(spec: str) -> list[tuple[str, str, str]]:
    """Parse fenced code blocks from *spec* and return (file_path, info_string, code).

    For each code block, looks at up to 5 lines of preceding context for
    a file path annotation (``# File:`` comment, a heading with a path,
    or a plain path-like string).  Returns only blocks where a file path
    could be determined.
    """
    import re as _re

    blocks: list[tuple[str, str, str]] = []

    for m in _FENCED_CODE_BLOCK_RE.finditer(spec):
        info = m.group(1).strip()
        code = m.group(2)

        # Find the fence opening position to locate preceding context.
        fence_start = m.start()
        before = spec[:fence_start]
        context_lines = before.split("\n")
        # Take up to 5 lines before the fence, reversed for priority.
        preceding = context_lines[-5:] if len(context_lines) >= 5 else context_lines

        file_path = ""
        for line in reversed(preceding):
            line = line.strip()
            if not line:
                continue
            # Try ``# File: path`` or ``// File: path`` annotation.
            fm = _re.match(r"[#/]{1,2}\s*File:\s*(\S+\.\w{1,10})", line)
            if fm:
                file_path = fm.group(1)
                break
            # Try a backtick-wrapped path in a heading or list item.
            bm = _re.search(r"`([^`]+\.\w{1,10})`", line)
            if bm:
                candidate = bm.group(1)
                if "/" in candidate and candidate.lower().endswith(
                    tuple(_SOURCE_EXTENSIONS)
                ):
                    file_path = candidate
                    break
            # Try a plain path-like string (must contain /).
            pm = _re.search(r"(\S+/\S+\.\w{1,10})", line)
            if pm:
                candidate = pm.group(1)
                if candidate.lower().endswith(tuple(_SOURCE_EXTENSIONS)):
                    file_path = candidate
                    break

        if file_path:
            blocks.append((file_path, info, code))

    return blocks


def _is_trivial_config_only_change(repo_dir: Path, target_branch: str) -> bool:
    """True when the change adds ONLY new config/presence files and is small.

    Returns ``True`` when ALL of:
    1. Every changed file has a config-only extension
       (:func:`_is_config_only_change`), AND
    2. The total diff delta (insertions + deletions) is ≤ 40 lines, AND
    3. At least one file is *new* (--diff-filter=A) — this is a fresh
       presence/config file, not a reconfiguration of existing code.

    Fail-closed: returns ``False`` on any git error, when the diff is
    empty, or when there are no new files.
    """
    if not _is_config_only_change(repo_dir, target_branch):
        return False

    # Check for at least one new (Added) file.
    added = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--name-only",
            "--diff-filter=A",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        return False
    if not added.stdout.strip():
        return False  # no new files → not a trivial addition

    # Check total diff size via --shortstat.
    stat = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--shortstat",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if stat.returncode != 0:
        return False
    shortstat = stat.stdout.strip()
    if not shortstat:
        return False

    import re as _re

    total = 0
    ins = _re.search(r"(\d+)\s+insertions?\(\+\)", shortstat)
    if ins:
        total += int(ins.group(1))
    dels = _re.search(r"(\d+)\s+deletions?\(-\)", shortstat)
    if dels:
        total += int(dels.group(1))

    if total == 0:
        return False

    return total <= 40


def _is_spec_exact_edits(spec: str, repo_dir: Path) -> bool:
    """True when *spec* contains fenced code blocks with file paths
    that all reference files existing in *repo_dir*.

    Returns ``True`` only when at least one code block maps to an
    existing file AND every referenced file exists on disk.  A single
    missing file fails the check — we fall through to the LLM path so
    the agent can diagnose the discrepancy.

    Fail-closed: returns ``False`` on any parse error or when no code
    blocks are found.
    """
    try:
        blocks = _parse_spec_code_blocks(spec)
    except Exception:
        return False

    if not blocks:
        return False

    for file_path, _info, _code in blocks:
        resolved = (repo_dir / file_path).resolve()
        if not resolved.is_relative_to(repo_dir.resolve()):
            return False
        if not resolved.is_file():
            return False

    return True


def _should_skip_test_gate(
    repo_dir: Path,
    target_branch: str,
    settings: Settings,
    ticket_summary: str,
) -> tuple[bool, str]:
    """Decide whether the full test gate can be skipped.

    Returns ``(skip, diag)`` where *skip* is ``True`` only when BOTH:
    1. The cheap deterministic ``_is_config_only_change`` check passes, AND
    2. The cheap LLM ``run_test_scope_agent`` confirms the diff cannot
       affect runtime behaviour and returns ``needs_full_suite=False``.

    In every other case — git error, mixed diff, no diff yet, missing API
    key, or an agent that asks for tests — the full deterministic suite
    runs and is the final arbiter.  The agent is consulted ONLY when the
    deterministic check already says config-only, so a real code change
    runs the full gate without ever paying for the LLM call.
    """
    # Trivial config-only additions (new presence/config files, ≤40 lines)
    # have zero behavioural delta — skip the full test suite without even
    # consulting the test-scope LLM agent.
    if _is_trivial_config_only_change(repo_dir, target_branch):
        return True, "trivial config-only addition — skipping full test gate"

    # Rename-only changes have zero behavioural delta — skip the full
    # test suite without even consulting the test-scope LLM agent.
    if _is_rename_only_change(repo_dir, target_branch):
        return True, "rename-only change — skipping full test gate"

    config_only = _is_config_only_change(repo_dir, target_branch)
    if not config_only:
        return False, "non-config files in diff — running full test gate"

    # Gather the inputs the agent needs: changed file list and diff stat
    # (both using the same ``git -C str(repo_dir)`` convention).
    changed_out = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    changed_files = (
        changed_out.stdout.strip().splitlines() if changed_out.returncode == 0 else []
    )

    stat_out = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "diff",
            "--stat",
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    diff_stat = stat_out.stdout.strip() if stat_out.returncode == 0 else ""

    from ...agents.test_scope import run_test_scope_agent

    verdict = run_test_scope_agent(
        settings=settings,
        changed_files=changed_files,
        diff_stat=diff_stat,
        ticket_summary=ticket_summary,
    )

    if verdict.needs_full_suite:
        return False, (
            f"config-only diff but agent assessed the change as behaviour-affecting "
            f"— running full test gate. Rationale: {verdict.rationale[:200]}"
        )

    return True, (
        f"config-only diff confirmed by agent as non-behavioural — "
        f"skipping full test gate. Rationale: {verdict.rationale[:200]}"
    )


# ---------------------------------------------------------------------------
# Internal dataclasses for the refactored implement loop
# ---------------------------------------------------------------------------


@dataclass
class _ImplementContext:
    """Artifact bundle loaded once before the fix loop starts."""

    spec: str
    memory_text: str
    reference_files: list[Any] | None
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
    new_msgs: bytes | None = None


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

    success: tuple[Any, ...] | None = None
    failure: _SinglePassResult | None = None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Spawn-state tracking (pre-LLM abort detection)
# ---------------------------------------------------------------------------

SPAWN_STATE_FILENAME = "implement_spawn_state.json"
SPAWN_ABORTS_FILENAME = "implement_spawn_aborts.jsonl"


def write_spawn_in_flight(
    artifacts_dir: Path,
    spawn_count: int,
    *,
    counted: bool,
) -> None:
    """Persist an *in-flight* marker so the next preflight can detect a
    process-death / SIGTERM kill that happened before the stage produced
    any outcome.
    """
    import json
    from datetime import datetime

    try:
        (artifacts_dir / SPAWN_STATE_FILENAME).write_text(
            json.dumps(
                {
                    "state": "in_flight",
                    "started_at": datetime.now(UTC).isoformat(),
                    "spawn_count": spawn_count,
                    "counted": counted,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        log.warning(
            "failed to write %s",
            artifacts_dir / SPAWN_STATE_FILENAME,
            exc_info=True,
        )


def clear_spawn_in_flight(artifacts_dir: Path) -> None:
    """Remove the in-flight marker after a completed implement attempt."""
    import contextlib

    with contextlib.suppress(OSError):
        (artifacts_dir / SPAWN_STATE_FILENAME).unlink(missing_ok=True)


def detect_and_absorb_killed_spawn(
    artifacts_dir: Path,
    counter_path: Path,
) -> str | None:
    """Detect a stale in-flight marker from a previous process lifetime.

    When the marker exists with ``state == "in_flight"`` the previous
    implement attempt died mid-flight (SIGTERM / crash) without
    recording any outcome.  This function:

    * Records the kill to ``implement_spawn_aborts.jsonl``.
    * When the killed attempt was *counted* (it consumed a spawn slot),
      decrements ``implement_spawn_count`` by one so process-death
      kills don't silently burn the ticket's spawn budget.
    * Removes the in-flight marker so it's not re-absorbed.

    Returns a one-line diagnostic string suitable for a block note when
    a killed spawn was detected, or ``None`` when the marker is absent
    or already cleared.
    """
    import json
    from datetime import datetime

    state_path = artifacts_dir / SPAWN_STATE_FILENAME
    if not state_path.exists():
        return None

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # fmt: skip
        # Corrupt / unreadable — clear it and move on.
        clear_spawn_in_flight(artifacts_dir)
        return None

    if state.get("state") != "in_flight":
        clear_spawn_in_flight(artifacts_dir)
        return None

    spawn_count: int = state.get("spawn_count", 0)
    counted: bool = state.get("counted", False)
    started_at: str = state.get("started_at", "unknown")

    # Absorb: decrement counter so process-death kills don't burn
    # the ticket's spawn budget.
    if counted:
        try:
            current = int(counter_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):  # fmt: skip
            current = 0
        corrected = max(current - 1, 0)
        try:
            counter_path.write_text(str(corrected), encoding="utf-8")
        except OSError:
            log.warning(
                "failed to write spawn counter after absorbing killed spawn",
                exc_info=True,
            )

    # Record the kill durably so spawn-limit blocks carry evidence.
    try:
        aborts_path = artifacts_dir / SPAWN_ABORTS_FILENAME
        with open(aborts_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "started_at": started_at,
                        "spawn_count": spawn_count,
                        "detected_at": datetime.now(UTC).isoformat(),
                        "counted": counted,
                    }
                )
                + "\n"
            )
    except OSError:
        log.warning(
            "failed to append to spawn aborts log",
            exc_info=True,
        )

    clear_spawn_in_flight(artifacts_dir)

    return (
        (
            f"Previous spawn attempt (started at {started_at}, "
            f"spawn #{spawn_count}) was killed by process shutdown/restart "
            f"before recording an outcome — {spawn_count} was "
            f"not consumed."
        )
        if counted
        else (
            f"Previous spawn attempt (started at {started_at}) was killed "
            f"by process shutdown/restart before recording an outcome "
            f"(retry, not counted)."
        )
    )


def read_spawn_aborts_tail(artifacts_dir: Path) -> str | None:
    """Return the tail of the spawn-aborts log for inclusion in a block
    note, or ``None`` when the log is absent or empty.
    """
    import json

    aborts_path = artifacts_dir / SPAWN_ABORTS_FILENAME
    if not aborts_path.exists():
        return None

    try:
        lines = aborts_path.read_text(encoding="utf-8").rstrip().splitlines()
    except OSError:
        return None

    if not lines:
        return None

    tail = lines[-5:]  # last 5 entries only
    entries = []
    for line in tail:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = {"_raw": line}
        entries.append(entry)

    note = "Spawn abort log (most recent first):"
    for e in reversed(entries):
        st = e.get("started_at", "unknown")
        sc = e.get("spawn_count", "?")
        c = "counted" if e.get("counted") else "not counted"
        note += f"\n- started {st}, spawn #{sc}, {c}"
    return note


# ---------------------------------------------------------------------------
# Zero-diff tracking (cross-spawn early-abort guard)
# ---------------------------------------------------------------------------

ZERO_DIFF_COUNT_FILENAME = "implement_zero_diff_count"
ZERO_DIFF_PAUSE_FILENAME = "implement_zero_diff_paused"


def read_zero_diff_count(artifacts_dir: Path) -> int:
    """Return the current consecutive-zero-diff count, or 0."""
    count_path = artifacts_dir / ZERO_DIFF_COUNT_FILENAME
    if not count_path.exists():
        return 0
    try:
        return int(count_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):  # fmt: skip
        log.warning(
            "failed to read %s — treating as 0",
            count_path,
            exc_info=True,
        )
        return 0


def write_zero_diff_count(artifacts_dir: Path, count: int) -> None:
    """Persist *count* to the zero-diff counter file."""
    count_path = artifacts_dir / ZERO_DIFF_COUNT_FILENAME
    try:
        count_path.write_text(str(count), encoding="utf-8")
    except OSError:
        log.warning(
            "failed to write %s",
            count_path,
            exc_info=True,
        )
