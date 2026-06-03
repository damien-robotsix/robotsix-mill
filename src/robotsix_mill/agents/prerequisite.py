"""Pre-implement prerequisite gate.

Verifies that external symbol/import prerequisites declared in a ticket
spec are satisfiable in the cloned repo's environment BEFORE the
expensive implement coordinator agent runs.  When a spec depends on a
symbol that must exist in an installed external dependency (e.g. an
unmerged ``robotsix_llmio`` port) and that symbol is not yet importable,
the ticket is short-circuited to BLOCKED — the work is still required
once the upstream symbol lands.

The gate is deterministic (no LLM call): it parses a machine-readable
``## Prerequisites`` / ````prereq```` block from the spec with regex and
runs a bounded subprocess check per directive in the repo's Python
environment.  Two directive forms are supported — the grammar is kept
small and conservative, like ``_SOURCE_EXTENSIONS`` in ``freshness.py``:

- ``import <module.dotted.path>`` — passes iff the module is importable.
- ``symbol <Name> from <module.dotted.path>`` — passes iff
  ``from <module> import <Name>`` succeeds.

Lines that don't match either form are ignored (forward-compatible).
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("robotsix_mill.agents.prerequisite")

# Total wall-clock budget for all prerequisite checks combined.
_TOTAL_TIMEOUT_S = 30

# Extract the ``## Prerequisites`` section body (up to the next
# top-level ``## `` heading or end of spec).
_SECTION_RE = re.compile(
    r"^[ \t]*##[ \t]+Prerequisites[ \t]*$\n(.*?)(?=^[ \t]*##[ \t]|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Within that section, capture the body of a single ````prereq```` fence.
_PREREQ_FENCE_RE = re.compile(
    r"```prereq[ \t]*\n(.*?)^[ \t]*```",
    re.MULTILINE | re.DOTALL,
)

# Directive grammar (kept deliberately small and conservative).
_IMPORT_RE = re.compile(r"^import\s+([\w.]+)$")
_SYMBOL_RE = re.compile(r"^symbol\s+(\w+)\s+from\s+([\w.]+)$")


def parse_prerequisites(spec: str) -> list[dict]:
    """Extract prerequisite directives from *spec*.

    Returns a list of ``{"directive": <normalized str>, "code": <python
    -c snippet>}`` dicts — one per recognized directive line inside the
    ````prereq```` block under the ``## Prerequisites`` heading.  Returns
    ``[]`` when the section or block is absent / empty, or when no line
    matches a known directive form (prose and other code fences are
    ignored).
    """
    section = _SECTION_RE.search(spec)
    if not section:
        return []
    fence = _PREREQ_FENCE_RE.search(section.group(1))
    if not fence:
        return []

    directives: list[dict] = []
    for line in fence.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        m = _IMPORT_RE.match(line)
        if m:
            module = m.group(1)
            directives.append(
                {"directive": f"import {module}", "code": f"import {module}"}
            )
            continue
        m = _SYMBOL_RE.match(line)
        if m:
            name, module = m.group(1), m.group(2)
            directives.append(
                {
                    "directive": f"symbol {name} from {module}",
                    "code": f"from {module} import {name}",
                }
            )
            continue
        # Unrecognized line — ignored (forward-compatible).
    return directives


def _default_runner(code: str, repo_dir: Path, timeout: float) -> int:
    """Run ``python -c <code>`` in *repo_dir* and return the exit code.

    A non-zero exit (an ImportError, a missing symbol, …) means the
    prerequisite is unmet.  Runs the repo's Python with ``cwd=repo_dir``
    so the installed dependency set (e.g. ``robotsix_llmio`` in
    site-packages) resolves exactly as it will during implement.
    """
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        log.debug(
            "prerequisite: `%s` exited %d: %s",
            code,
            proc.returncode,
            (proc.stdout + proc.stderr).strip()[:300],
        )
    return proc.returncode


def run_prerequisite_check(
    spec: str,
    repo_dir: Path | None,
    *,
    runner=_default_runner,
) -> dict:
    """Verify external symbol/import prerequisites declared in *spec*.

    Returns ``{"unmet": [<directive strings>], "reason": <summary>}``.
    An empty ``unmet`` list means the gate passed (proceed).  Returns
    early (no unmet) when the ``## Prerequisites`` section is absent or
    empty — most specs declare no prerequisites.

    Degrades gracefully: on ANY error the gate returns an empty
    ``unmet`` so a checker fault never blocks a ticket — exactly as
    ``freshness.run_freshness_check`` does.
    """
    try:
        directives = parse_prerequisites(spec)
    except Exception:
        log.warning("prerequisite: parse failed", exc_info=True)
        return {"unmet": [], "reason": "parse failed"}

    if not directives:
        return {"unmet": [], "reason": "no prerequisites declared"}

    if repo_dir is None:
        return {"unmet": [], "reason": "no repo — cannot verify prerequisites"}

    # Split the total budget evenly so the gate stays bounded regardless
    # of how many directives are declared.
    per_check_timeout = max(1.0, _TOTAL_TIMEOUT_S / len(directives))
    unmet: list[str] = []
    for d in directives:
        try:
            rc = runner(d["code"], repo_dir, per_check_timeout)
        except Exception:
            log.warning(
                "prerequisite: check for `%s` errored — treating as met",
                d["directive"],
                exc_info=True,
            )
            continue
        if rc != 0:
            unmet.append(d["directive"])

    if unmet:
        return {
            "unmet": unmet,
            "reason": "unmet prerequisite(s): " + ", ".join(unmet),
        }
    return {
        "unmet": [],
        "reason": f"all {len(directives)} prerequisite(s) satisfied",
    }
