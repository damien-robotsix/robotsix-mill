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

Directives whose module top-level package matches the target repo's own
package name are skipped (treated as met, with a log line) — same-repo
symbols are deliverables, not prerequisites.
"""

from __future__ import annotations

import base64
import logging
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .. import sandbox
from ..config import Settings

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


def _top_level_module(dotted: str) -> str:
    """Return the top-level package of a dotted module path.

    ``robotsix_auto_mail.db`` → ``robotsix_auto_mail``.
    ``os.path`` → ``os``.
    A bare ``foo`` → ``foo``.
    """
    return dotted.split(".")[0]


def _repo_package_names(repo_dir: Path) -> set[str]:
    """Derive the target repo's own Python package name(s).

    Reads ``pyproject.toml`` → ``[project].name`` (distribution name,
    e.g. ``robotsix-auto-mail``) and converts to the import name form
    (e.g. ``robotsix_auto_mail``).  Also discovers top-level packages
    under ``src/`` as a fallback when ``pyproject.toml`` is absent.

    Returns an empty set when the package name cannot be determined.
    """
    names: set[str] = set()
    pp = repo_dir / "pyproject.toml"
    if pp.is_file():
        try:
            import tomllib

            data = tomllib.loads(pp.read_text(encoding="utf-8"))
            proj = data.get("project", {}) or {}
            pkg_name = str(proj.get("name", ""))
            if pkg_name:
                names.add(pkg_name.replace("-", "_").lower())
        except Exception:
            log.debug(
                "prerequisite: could not read package name from %s",
                pp,
                exc_info=True,
            )

    # Fallback: top-level directories under src/ that look like packages.
    src_dir = repo_dir / "src"
    if src_dir.is_dir():
        for child in src_dir.iterdir():
            if child.is_dir() and (child / "__init__.py").is_file():
                names.add(child.name.lower())

    return names


def parse_prerequisites(spec: str) -> list[dict[str, str]]:
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

    directives: list[dict[str, str]] = []
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


def _build_batch_script(directives: list[dict[str, str]]) -> str:
    """Build a single stdlib-only Python script that checks every directive.

    Each directive runs in its OWN ``try/except ImportError`` block (no
    target module is imported at the top level of the generated script),
    printing ``PREREQ_OK:<i>`` on success and ``PREREQ_FAIL:<i>`` when the
    import / symbol resolution fails.  The indices line up positionally
    with *directives* so the caller can map failures back to directive
    strings.
    """
    lines: list[str] = []
    for i, d in enumerate(directives):
        lines.append("try:")
        lines.append(f"    {d['code']}")
        lines.append(f"    print('PREREQ_OK:{i}')")
        lines.append("except ImportError:")
        lines.append(f"    print('PREREQ_FAIL:{i}')")
    return "\n".join(lines) + "\n"


def _sandbox_batch_check(
    directives: list[dict[str, str]],
    repo_dir: Path,
    settings: Settings,
    sandbox_image: str | None,
) -> tuple[list[str], str | None]:
    """Check every directive inside the target repo's own environment.

    Runs ONE sandbox container with ``install_project=True`` so the
    repo's declared dependencies (``pip install .``) are installed once
    before the batch script executes — resolving cross-repo symbols
    against the *target repo's* deps rather than the mill's
    site-packages.  Returns ``(unmet, error)``:

    - ``unmet`` is the list of directive strings that failed.
    - ``error`` is ``"sandbox unavailable"`` on a :class:`SandboxError`
      (the gate then proceeds — a checker fault never blocks), else
      ``None``.

    On a non-zero exit with NO markers in the output the script crashed
    before reporting anything; we conservatively treat ALL directives as
    unmet.
    """
    script = _build_batch_script(directives)
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    # Decode + exec the stdlib-only script via base64 so no shell quoting
    # of the multi-line source is needed.
    command = (
        f"python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode())\""
    )
    try:
        rc, output = sandbox.run(
            command,
            repo_dir=repo_dir,
            settings=settings,
            install_project=True,
            sandbox_image=sandbox_image,
        )
    except sandbox.SandboxError:
        log.warning("prerequisite: sandbox unavailable — proceeding", exc_info=True)
        return [], "sandbox unavailable"

    fail_indices: set[int] = set()
    has_markers = False
    for m in re.finditer(r"PREREQ_(OK|FAIL):(\d+)", output):
        has_markers = True
        if m.group(1) == "FAIL":
            fail_indices.add(int(m.group(2)))

    if rc != 0 and not has_markers:
        # Script crashed before reporting — conservative: all unmet.
        return [d["directive"] for d in directives], None

    unmet = [
        directives[i]["directive"] for i in range(len(directives)) if i in fail_indices
    ]
    return unmet, None


def _filter_same_repo(
    directives: list[dict[str, str]], repo_pkgs: set[str]
) -> list[dict[str, str]]:
    """Return only the directives whose top-level module is NOT a
    same-repo package, logging each skipped directive at INFO level."""
    external: list[dict[str, str]] = []
    for d in directives:
        code = d["code"]
        # Extract the module path: "import foo.bar" → "foo.bar",
        # "from foo.bar import Baz" → "foo.bar".
        if code.startswith("import "):
            mod_path = code[7:]
        elif code.startswith("from "):
            mod_path = code[5 : code.index(" import")]
        else:
            mod_path = ""
        top = _top_level_module(mod_path)
        if repo_pkgs and top in repo_pkgs:
            log.info(
                "prerequisite: skipping same-repo directive `%s` "
                "(top-level %r matches repo packages %s)",
                d["directive"],
                top,
                repo_pkgs,
            )
            continue
        external.append(d)
    return external


def _run_individual_checks(
    external: list[dict[str, str]],
    repo_dir: Path,
    runner: Callable[[str, Path, float], int],
) -> list[str]:
    """Run each directive one at a time via *runner*, returning the
    ``unmet`` list."""
    per_check_timeout = max(1.0, _TOTAL_TIMEOUT_S / len(external)) if external else 1.0
    unmet = []
    for d in external:
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
    return unmet


def run_prerequisite_check(
    spec: str,
    repo_dir: Path | None,
    *,
    runner: Callable[[str, Path, float], int] = _default_runner,
    settings: Settings | None = None,
    sandbox_image: str | None = None,
) -> dict[str, list[str] | str]:
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

    # Skip directives whose top-level module matches the target repo's
    # own package — same-repo symbols are deliverables, not prerequisites.
    repo_pkgs = _repo_package_names(repo_dir)
    external = _filter_same_repo(directives, repo_pkgs)

    if not external:
        return {
            "unmet": [],
            "reason": f"all {len(directives)} prerequisite(s) are same-repo — skipped",
        }

    # Production path: when the runner is the default AND settings are
    # available, verify against the TARGET repo's own environment — the
    # sandbox installs its declared deps (``pip install .``) before the
    # batch check runs, so cross-repo symbols resolve correctly. The
    # whole batch runs in a single container (one ``pip install .``).
    if runner is _default_runner and settings is not None:
        unmet, error = _sandbox_batch_check(external, repo_dir, settings, sandbox_image)
        if unmet:
            return {
                "unmet": unmet,
                "reason": "unmet prerequisite(s): " + ", ".join(unmet),
            }
        return {
            "unmet": [],
            "reason": f"all {len(external)} prerequisite(s) satisfied",
        }

    # Split the total budget evenly so the gate stays bounded regardless
    # of how many directives are declared.
    unmet = _run_individual_checks(external, repo_dir, runner)

    if unmet:
        return {
            "unmet": unmet,
            "reason": "unmet prerequisite(s): " + ", ".join(unmet),
        }
    return {
        "unmet": [],
        "reason": f"all {len(external)} prerequisite(s) satisfied",
    }
