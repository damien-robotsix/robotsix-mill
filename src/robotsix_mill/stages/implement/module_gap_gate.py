"""Pre-implement staleness gate for module_curator ``unclassified:`` gaps.

A module_curator "Classify `<path>`" ticket carries a
``<!-- module_curator-gap-id: unclassified:<path> -->`` marker.  By the
time it reaches implement, a sibling may already have claimed the path in
``docs/modules.yaml`` (or the path may be gone), so the implement agent
would only confirm "already claimed" with an empty diff.  This gate
re-runs the module glob match against ``origin/<target>`` and closes the
ticket as superseded before any implement trace opens.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from ...agents.expert_manager import ExpertManager
from ...core.constants import OBSOLESCENCE_GAP_PREFIX

MODULES_YAML = "docs/modules.yaml"

_UNCLASSIFIED_GAP_RE = re.compile(
    r"<!--\s*module_curator-gap-id:\s*unclassified:(\S+?)\s*-->"
)


def parse_unclassified_gap_path(text: str) -> str | None:
    """Return the ``<path>`` of a ``module_curator-gap-id: unclassified:<path>``
    marker in *text*, or ``None`` when absent.
    """
    m = _UNCLASSIFIED_GAP_RE.search(text or "")
    if m is None:
        return None
    path = m.group(1).strip().strip("`").removeprefix("./")
    return path or None


def find_claiming_module(modules_yaml_text: str, path: str) -> str | None:
    """Return the id of the first module whose ``paths`` claim *path*.

    A path entry claims *path* when its glob fully matches it, or when the
    entry names a parent directory of it.  Returns ``None`` when no module
    claims it or the YAML cannot be parsed.
    """
    try:
        data = yaml.safe_load(modules_yaml_text)
    except yaml.YAMLError:
        return None
    modules = data.get("modules", []) if isinstance(data, dict) else []
    for mod in modules or []:
        if not isinstance(mod, dict):
            continue
        patterns = [p for p in mod.get("paths") or [] if isinstance(p, str)]
        if ExpertManager.match_module_paths(patterns, path):
            return str(mod.get("id", "")) or None
        for pattern in patterns:
            prefix = pattern.rstrip("/")
            if (
                prefix
                and not any(c in prefix for c in "*?[")
                and (path.startswith(prefix + "/"))
            ):
                return str(mod.get("id", "")) or None
    return None


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return None


def check_unclassified_gap(repo_dir: Path, target_branch: str, spec: str) -> str | None:
    """Return a terminal close note when the spec's ``unclassified:<path>``
    gap is already resolved on ``origin/<target_branch>``, else ``None``.

    Resolved means: the path no longer exists on the target branch, or a
    module in ``docs/modules.yaml`` already claims it.  Conservative —
    returns ``None`` on any git/parse failure so a real gap is never
    skipped.
    """
    path = parse_unclassified_gap_path(spec)
    if path is None:
        return None
    ref = f"origin/{target_branch}"

    exists = _git(repo_dir, "cat-file", "-e", f"{ref}:{path}")
    if exists is None:
        return None
    if exists.returncode != 0:
        # Distinguish "path gone" from "ref missing / not a repo".
        ref_ok = _git(repo_dir, "rev-parse", "--verify", "--quiet", ref)
        if ref_ok is None or ref_ok.returncode != 0:
            return None
        return (
            f"{OBSOLESCENCE_GAP_PREFIX}: `{path}` no longer exists on {ref} "
            "— superseded, implement skipped"
        )

    shown = _git(repo_dir, "show", f"{ref}:{MODULES_YAML}")
    if shown is None or shown.returncode != 0:
        return None
    module_id = find_claiming_module(shown.stdout, path)
    if module_id is None:
        return None
    return (
        f"{OBSOLESCENCE_GAP_PREFIX}: `{path}` is already claimed by module "
        f"`{module_id}` in {MODULES_YAML} on {ref} — superseded, implement "
        "skipped"
    )
