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
from typing import TYPE_CHECKING, Literal

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
                    exc_info=True,
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
    settings: "Settings",
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

    success: tuple | None = None
    failure: _SinglePassResult | None = None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------
