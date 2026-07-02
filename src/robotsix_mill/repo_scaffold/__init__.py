"""Workflow that scaffolds brand-new repositories created via the
maintenance agent's ``create_repo`` tool.

The maintenance agent calls :func:`run_repo_scaffold` with the creation
params and the raw ticket description.  This module scaffolds an initial
commit, appends a ``RepoConfig`` entry to the machine-owned overlay, and
files a build-out ticket on the new repo's board.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..config import Settings
    from ..forge.base import Forge
    from ..stages.base import Outcome, StageContext

from ..config import _reset_repos_config
from ..core.states import State
from ..forge.auth import github_token
from ..forge.base import NotConfiguredError, RepoInfo
from ..vcs import git_ops

log = logging.getLogger("robotsix_mill.repo_scaffold")

# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------


def run_repo_scaffold(
    settings: Settings,
    forge: Forge,
    ctx: StageContext,
    params: dict[str, Any],
    ticket_description: str = "",
) -> Outcome:
    """Execute the repo-scaffold workflow for a ``new-repo`` extraction ticket.

    Parameters:
        settings: Mill :class:`~robotsix_mill.config.Settings`.
        forge: A :class:`~robotsix_mill.forge.base.Forge` instance.
        ctx: The :class:`~robotsix_mill.stages.base.StageContext`.
        params: Dict with keys ``name``, ``owner``, ``private``,
            ``description``, ``language``.
        ticket_description: The raw ticket body (no marker).

    Returns:
        :class:`Outcome` — ``DONE`` on success, ``BLOCKED`` when repo
        creation is unavailable or the repo already exists, ``ERRORED``
        on unexpected failures.
    """
    from ..stages.base import Outcome

    try:
        repo_info = forge.create_repo(
            name=params["name"],
            owner=params["owner"],
            private=params["private"],
            description=params["description"],
        )
    except NotConfiguredError:
        return Outcome(State.BLOCKED, note="Repo creation not configured")

    except RuntimeError as exc:
        if "already exists" in str(exc).lower():
            return Outcome(
                State.BLOCKED, note=f"Repo {params['name']!r} already exists"
            )
        raise

    try:
        _scaffold_initial_commit(settings, repo_info, params)
    except Exception:
        log.exception("Scaffold commit failed for %s", repo_info.name)
        return Outcome(State.ERRORED, note="Scaffold commit failed")

    try:
        _append_repo_config(repo_info, params, settings)
    except Exception:
        log.exception("repos overlay append failed for %s", repo_info.name)
        return Outcome(State.ERRORED, note="repos overlay append failed")

    # The scaffold only creates an EMPTY repo. File a build-out ticket on the
    # new repo's own board so the normal pipeline (refine → epic-breakdown →
    # implement) populates it with its actual purpose. Best-effort: a failure
    # here doesn't undo the (succeeded) repo creation + registration.
    followup_id = _file_implementation_followup(
        settings, repo_info, params, ticket_description
    )
    note = (
        f"created + registered {repo_info.name}; filed build-out ticket {followup_id}"
        if followup_id
        else f"created + registered {repo_info.name}"
    )
    return Outcome(State.DONE, note=note)


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------


def _file_implementation_followup(
    settings: Settings,
    repo_info: RepoInfo,
    params: dict[str, Any],
    scaffold_description: str,
) -> str | None:
    """File a build-out ticket on the NEW repo's own board.

    The scaffold creates an empty repo; this kicks off its actual
    implementation through the normal pipeline (refine → epic-breakdown →
    implement) on ``board_id = sanitize(repo_name)``. The spec is derived
    from the scaffold ticket's purpose (its description minus the new-repo
    marker). Best-effort — returns the new ticket id, or ``None`` on
    failure (the caller does not fail the scaffold over this).
    """
    from ..core.models import SourceKind
    from ..core.service import TicketService

    name = repo_info.name
    board_id = _sanitize_repo_id(name)
    # Purpose = the scaffold ticket body with the marker stripped out.
    purpose = (scaffold_description or "").strip()
    if not purpose:
        purpose = params.get("description", "") or f"Build out the {name} library."

    body = (
        f"The repository **{name}** was just scaffolded (empty: README, "
        f"LICENSE, language skeleton) and registered in the repos overlay. "
        f"It now needs its actual implementation.\n\n"
        f"## Purpose\n\n{purpose}\n\n"
        f"## Scope\n\n"
        f"Build out {name} per the purpose above: move/author the code, add "
        f"tests, and make it installable. If this is an extraction from "
        f"robotsix-mill, port the relevant modules + their tests into this "
        f"repo and keep the public API stable. Large enough to split → let "
        f"epic-breakdown decompose it."
    )
    try:
        svc = TicketService(settings, board_id=board_id)
        ticket = svc.create(
            title=f"Implement {name}: initial build-out",
            description=body,
            source=SourceKind.AGENT,
        )
        log.info(
            "repo_scaffold: filed build-out ticket %s on board %s",
            ticket.id,
            board_id,
        )
        return ticket.id
    except Exception:
        log.exception("repo_scaffold: failed to file build-out ticket for %s", name)
        return None


def _scaffold_initial_commit(
    settings: Settings, repo_info: RepoInfo, params: dict[str, Any]
) -> None:
    """Initialise a fresh local repo, write scaffold files, commit, and
    force-push to the new remote's default branch.

    The remote was just created with ``auto_init: false`` so it is empty —
    cloning ``--branch main`` would fail (no branch yet). ``git init`` + a
    force-push populates the default branch directly. Use the repo-creation
    PAT when set: it created the repo and definitely has push access, whereas
    the GitHub App installation may not yet include the brand-new repo.
    """
    from ..config import get_secrets

    token = get_secrets().forge_repo_create_token or github_token(
        settings, repo_config=None
    )
    name = params["name"]
    description = params.get("description", "")

    workspace_dir = Path(tempfile.mkdtemp(prefix="repo_scaffold_"))
    try:
        git_ops.init_repo(workspace_dir, settings.forge_target_branch)

        # README.md
        (workspace_dir / "README.md").write_text(
            f"# {name}\n\n{description}\n", encoding="utf-8"
        )

        # LICENSE (MIT — same as robotsix-mill repo root)
        _write_license(workspace_dir)

        # Language skeleton
        language = params.get("language", "python")
        if language == "python":
            _write_python_skeleton(workspace_dir, name)
            # Reusable-workflow callers (CI + docs). Only python repos get
            # these — the shared workflows are `python-*`.
            _write_github_workflows(workspace_dir)

        # Per-repo mill config: the repo owns its test_command + languages in
        # its own .robotsix-mill/config.yaml (not the operator's repos.yaml).
        _write_repo_config(workspace_dir, language)

        # Periodic agents: enablement is file presence, so the new repo
        # opts into the default set by carrying these in its first commit.
        _write_periodic_presence_files(workspace_dir)

        git_ops.commit_all(workspace_dir, "Initial scaffold")

        git_ops.push(
            workspace_dir,
            settings.forge_target_branch,
            repo_info.clone_url,
            token,
        )
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)


def _write_license(repo_dir: Path) -> None:
    """Write the MIT license (same text as the robotsix-mill repo root LICENSE)."""
    license_text = """MIT License

Copyright (c) 2026 Damien Robotsix

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
    (repo_dir / "LICENSE").write_text(license_text, encoding="utf-8")


def _python_package_name(name: str) -> str:
    """Convert a distribution name into an import-safe package name.

    Python package directories must be valid identifiers — they cannot
    contain hyphens. A repo named ``robotsix-board`` ships the
    distribution ``robotsix-board`` but the import package is
    ``robotsix_board``. Using the raw (hyphenated) name as the ``src/``
    directory makes hatchling's wheel build fail with "Unable to
    determine which files to ship inside the wheel", which then poisons
    the new repo's baseline check and blocks every ticket.
    """
    return name.replace("-", "_")


def _write_python_skeleton(repo_dir: Path, name: str) -> None:
    """Write a minimal Python project skeleton: ``pyproject.toml``,
    ``src/{pkg}/__init__.py`` (``pkg`` = import-safe name), and
    ``tests/__init__.py``. The wheel target is declared explicitly so
    hatchling can always resolve the package regardless of how it
    normalizes the distribution name."""
    pkg_name = _python_package_name(name)
    pyproject = f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"

[tool.hatch.build.targets.wheel]
packages = ["src/{pkg_name}"]
"""
    pyproject_path = repo_dir / "pyproject.toml"
    pyproject_path.write_text(pyproject, encoding="utf-8")

    # Validate the generated TOML before proceeding — a corrupted
    # file blocks every downstream tool (ruff, pytest, uv) with
    # cryptic parse errors.
    try:
        tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Generated pyproject.toml is invalid TOML: {exc}") from exc

    pkg_dir = repo_dir / "src" / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")


def _write_github_workflows(repo_dir: Path) -> None:
    """Write the reusable-workflow caller files for a new python member repo.

    Mill hosts the shared reusable workflows, so its OWN callers reference
    them via the LOCAL path ``./.github/workflows/python-ci.yml``. A member
    repo must instead use the CROSS-REPO form
    ``damien-robotsix/robotsix-mill/.github/workflows/python-ci.yml@main``
    (resp. ``python-docs.yml``). Generating these by construction avoids the
    two recurring hand-authoring mistakes that produce a ``startup_failure``:
    the wrong org (``robotsix`` instead of ``damien-robotsix``) and a calling
    job that grants no ``permissions:`` block.
    """
    workflows_dir = repo_dir / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    ci_yml = """name: CI

on: [pull_request, push]

permissions:
  contents: read

jobs:
  ci:
    permissions:
      contents: read
      security-events: write
    uses: damien-robotsix/robotsix-mill/.github/workflows/python-ci.yml@main
"""
    (workflows_dir / "ci.yml").write_text(ci_yml, encoding="utf-8")

    docs_yml = """name: Docs

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  deploy:
    permissions:
      contents: write
    uses: damien-robotsix/robotsix-mill/.github/workflows/python-docs.yml@main
"""
    (workflows_dir / "docs.yml").write_text(docs_yml, encoding="utf-8")


def _write_repo_config(repo_dir: Path, language: str) -> None:
    """Write the new repo's ``.robotsix-mill/config.yaml`` declaring its
    ``test_command`` + ``languages`` — the repo owns these (not repos.yaml)."""
    cfg_dir = repo_dir / ".robotsix-mill"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    test_command = "pytest -q" if language == "python" else ""
    lines = [f"test_command: {test_command}".rstrip()]
    if language:
        lines.append(f"languages: [{language}]")
    (cfg_dir / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_periodic_presence_files(repo_dir: Path) -> None:
    """Write the default per-repo periodic presence files into the new repo.

    Periodic agents are enabled by the presence of
    ``.robotsix-mill/periodic/<name>.yaml`` in the repo (a name-only file
    inherits the built-in's params). Scaffolding them here is what actually
    turns audit + health on for a brand-new repo — the repos.yaml stanza no
    longer carries periodic config.
    """
    periodic_dir = repo_dir / ".robotsix-mill" / "periodic"
    periodic_dir.mkdir(parents=True, exist_ok=True)
    for name in _DEFAULT_PERIODIC_NAMES:
        (periodic_dir / f"{name}.yaml").write_text(
            f"# Per-repo periodic workflow: presence enables it for this repo.\n"
            f"# Name-only → inherits the built-in {name} params.\n"
            f"name: {name}\n",
            encoding="utf-8",
        )


def _sanitize_repo_id(name: str) -> str:
    """Derive a safe ``repo_id`` from a repo name: lowercase, hyphens
    for non-alphanumeric characters."""
    sanitized = []
    for ch in name.lower():
        if ch.isascii() and ch.isalnum():
            sanitized.append(ch)
        elif ch == "-":
            sanitized.append("-")
        elif ch in (" ", "_", ".") or not ch.isascii():
            sanitized.append("-")
        # drop other characters
    return "".join(sanitized).strip("-") or name.lower()


def _repos_yaml_path(settings: "Settings | None" = None) -> Path | None:
    """Resolve the machine-owned overlay path for auto-registered repos.

    Returns ``None`` when ``MILL_REPOS_FILE`` is explicitly empty
    (disabled by test suite).
    """
    path_str = os.environ.get("MILL_REPOS_FILE")
    if path_str is not None:
        if path_str == "":
            return None
        return Path(path_str)
    # Machine-owned overlay in the writable data volume.
    data_dir = settings.data_dir if settings is not None else Path(".data")
    return Path(data_dir) / "registered_repos.yaml"


# Periodic agents the scaffold enables on a brand-new repo, written as
# name-only presence files into its initial commit (see
# _write_periodic_presence_files). Enablement is per-repo file presence
# (.robotsix-mill/periodic/<name>.yaml) — NOT a repos.yaml block, which
# the loader no longer reads.
_DEFAULT_PERIODIC_NAMES = ("audit", "health")


def _append_repo_config(
    repo_info: RepoInfo, params: dict[str, Any], settings: Settings
) -> None:
    """Append a :class:`RepoConfig` stanza to the machine-owned overlay
    for the newly created repository."""
    path = _repos_yaml_path(settings)
    if path is None:
        log.info("MILL_REPOS_FILE is empty — skipping repos.yaml append")
        return

    # Load existing YAML
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    else:
        data = {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if "repos" not in data:
        data["repos"] = {}

    repo_id = _sanitize_repo_id(repo_info.name)

    # Langfuse is configured globally (top-level ``langfuse`` block in
    # repos.yaml); the new repo inherits it automatically — no per-repo
    # langfuse stanza is written.
    new_entry: dict[str, Any] = {
        "board_id": repo_id,
        "forge_remote_url": repo_info.clone_url,
        # Per-repo `test_command` + `language(s)` are NOT configured here —
        # they live in the new repo's own .robotsix-mill/config.yaml (written
        # into the initial scaffold commit, see _write_repo_config). Periodic
        # agents likewise opt in via .robotsix-mill/periodic/ file presence.
    }

    data["repos"][repo_id] = new_entry

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)

    _reset_repos_config()
    log.info("Appended repo %r to %s", repo_id, path)
