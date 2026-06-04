"""Workflow that acts on meta-board ``new-repo`` extraction proposals.

When the implement stage encounters a ticket with ``source=META`` and a
``<!-- meta-extraction-kind: new-repo ... -->`` marker in its
description, it routes to this module instead of the normal implement
loop.  This module calls :meth:`Forge.create_repo`, scaffolds an initial
commit, appends a ``RepoConfig`` entry to ``config/repos.yaml``, and
transitions the ticket to ``DONE``.
"""

from __future__ import annotations

import logging
import os
import re as _re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .stages.base import Outcome

from .config import _reset_repos_config, get_repos_config
from .core.states import State
from .forge.auth import github_token
from .forge.base import NotConfiguredError, RepoInfo
from .vcs import git_ops

log = logging.getLogger("robotsix_mill.repo_scaffold")

MARKER_KIND = "new-repo"

_MARKER_RE = _re.compile(
    r"<!--\s*(meta-extraction-kind:\s*new-repo.*?)-->",
    _re.DOTALL,
)

# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------


def parse_new_repo_params(description: str) -> dict | None:
    """Scan *description* for a ``new-repo`` extraction marker block.

    Returns a dict with keys ``name``, ``owner``, ``private``,
    ``description``, and ``language`` when the marker is present and
    parseable; ``None`` otherwise.  ``private`` defaults to ``True``,
    ``language`` defaults to ``"python"``.
    """
    m = _MARKER_RE.search(description)
    if not m:
        return None

    yaml_body = m.group(1)
    # The first line inside the comment is ``meta-extraction-kind: new-repo``
    # (the marker kind itself).  The remaining indented lines are the
    # YAML payload.  Strip that header line and parse the rest.
    lines = yaml_body.splitlines()
    if not lines:
        return None
    first = lines[0].strip()
    if not first.startswith("meta-extraction-kind:") or "new-repo" not in first:
        return None

    payload = "\n".join(lines[1:])
    if not payload.strip():
        log.warning("new-repo marker has no payload fields")
        return None

    try:
        data = yaml.safe_load(payload)
    except yaml.YAMLError:
        log.warning("Failed to parse new-repo marker YAML in ticket description")
        return None

    if not isinstance(data, dict):
        return None

    name = data.get("name")
    if not name or not isinstance(name, str):
        log.warning("new-repo marker missing required 'name' field")
        return None

    return {
        "name": name.strip(),
        "owner": str(data.get("owner", "")).strip(),
        "private": bool(data.get("private", True)),
        "description": str(data.get("description", "")).strip(),
        "language": str(data.get("language", "python")).strip().lower(),
    }


def build_new_repo_marker(
    name: str,
    *,
    owner: str = "",
    private: bool = False,
    description: str = "",
    language: str = "python",
) -> str:
    """Build the canonical ``new-repo`` extraction marker block.

    The single source of truth for the marker FORMAT (the counterpart to
    :func:`parse_new_repo_params`, which reads it back). Producers — e.g.
    the refine ``request_new_repo`` tool — call this so the marker is
    always valid YAML regardless of how the agent phrased its request.
    Round-trips: ``parse_new_repo_params(build_new_repo_marker(x)) == x``.
    """
    data = {
        "name": name,
        "owner": owner,
        "private": bool(private),
        "description": description,
        "language": language,
    }
    payload = yaml.safe_dump(data, default_flow_style=False, sort_keys=False).strip()
    indented = "\n".join(f"  {line}" for line in payload.splitlines())
    return f"<!-- meta-extraction-kind: new-repo\n{indented}\n-->"


def run_repo_scaffold(settings, ticket, forge, ctx) -> Outcome:
    """Execute the repo-scaffold workflow for a ``new-repo`` extraction ticket.

    Parameters:
        settings: Mill :class:`~robotsix_mill.config.Settings`.
        ticket: The meta-board :class:`~robotsix_mill.core.models.Ticket`.
        forge: A :class:`~robotsix_mill.forge.base.Forge` instance.
        ctx: The :class:`~robotsix_mill.stages.base.StageContext`.

    Returns:
        :class:`Outcome` — ``DONE`` on success, ``BLOCKED`` when repo
        creation is unavailable or the repo already exists, ``ERRORED``
        on unexpected failures.
    """
    from .stages.base import Outcome

    description = ctx.service.workspace(ticket).read_description()
    params = parse_new_repo_params(description)
    if params is None:
        return Outcome(
            State.ERRORED,
            note="Could not parse new-repo params from ticket description",
        )

    try:
        repo_info = forge.create_repo(
            name=params["name"],
            owner=params["owner"],
            private=params["private"],
            description=params["description"],
        )
    except NotConfiguredError:
        _post_blocked_comment(
            ctx,
            ticket,
            f"## Repo creation is not configured\n\n"
            f"The meta-agent proposed creating a new repository "
            f"**{params['name']}** (owner: `{params['owner'] or '(not specified)'}`), "
            f"but repo creation is currently disabled.\n\n"
            f"### Manual steps\n"
            f"1. Create the repo `{params['name']}` under `{params['owner'] or 'your user/org'}` "
            f"on the forge.\n"
            f"2. Add an entry for `{params['name']}` to `config/repos.yaml`.\n"
            f"3. Close this ticket manually.",
        )
        return Outcome(State.BLOCKED, note="Repo creation not configured")

    except RuntimeError as exc:
        if "already exists" in str(exc).lower():
            _post_blocked_comment(
                ctx,
                ticket,
                f"## Repo already exists\n\n"
                f"The repository **{params['name']}** already exists on the forge. "
                f"This ticket cannot be fulfilled automatically.\n\n"
                f"### Next steps\n"
                f"Either choose a different name or close this ticket if the repo "
                f"was created manually.",
            )
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
        log.exception("config/repos.yaml append failed for %s", repo_info.name)
        return Outcome(State.ERRORED, note="config/repos.yaml append failed")

    # The scaffold only creates an EMPTY repo. File a build-out ticket on the
    # new repo's own board so the normal pipeline (refine → epic-breakdown →
    # implement) populates it with its actual purpose. Best-effort: a failure
    # here doesn't undo the (succeeded) repo creation + registration.
    followup_id = _file_implementation_followup(
        settings, repo_info, params, description
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
    settings, repo_info: RepoInfo, params: dict, scaffold_description: str
) -> str | None:
    """File a build-out ticket on the NEW repo's own board.

    The scaffold creates an empty repo; this kicks off its actual
    implementation through the normal pipeline (refine → epic-breakdown →
    implement) on ``board_id = sanitize(repo_name)``. The spec is derived
    from the scaffold ticket's purpose (its description minus the new-repo
    marker). Best-effort — returns the new ticket id, or ``None`` on
    failure (the caller does not fail the scaffold over this).
    """
    from .core.models import SourceKind
    from .core.service import TicketService

    name = repo_info.name
    board_id = _sanitize_repo_id(name)
    # Purpose = the scaffold ticket body with the marker stripped out.
    purpose = _MARKER_RE.sub("", scaffold_description or "").strip()
    if not purpose:
        purpose = params.get("description", "") or f"Build out the {name} library."

    body = (
        f"The repository **{name}** was just scaffolded (empty: README, "
        f"LICENSE, language skeleton) and registered in `config/repos.yaml`. "
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


def _scaffold_initial_commit(settings, repo_info: RepoInfo, params: dict) -> None:
    """Clone the newly created repo, write scaffold files, commit, and
    force-push to the default branch."""
    token = github_token(settings, repo_config=None)
    name = params["name"]
    description = params.get("description", "")

    workspace_dir = Path(tempfile.mkdtemp(prefix="repo_scaffold_"))
    try:
        git_ops.clone(
            remote_url=repo_info.clone_url,
            dest=workspace_dir,
            branch=settings.forge_target_branch,
            token=token,
        )

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


def _write_python_skeleton(repo_dir: Path, name: str) -> None:
    """Write a minimal Python project skeleton: ``pyproject.toml``,
    ``src/{name}/__init__.py``, and ``tests/__init__.py``."""
    pyproject = f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
"""
    (repo_dir / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    pkg_dir = repo_dir / "src" / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")


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


def _repos_yaml_path() -> Path | None:
    """Resolve the ``config/repos.yaml`` path using the same logic as
    :func:`~robotsix_mill.config_loader.load_repos_yaml`.

    Returns ``None`` when ``MILL_REPOS_FILE`` is explicitly empty
    (disabled by test suite).
    """
    path_str = os.environ.get("MILL_REPOS_FILE")
    if path_str is not None:
        if path_str == "":
            return None
        return Path(path_str)
    return Path("config/repos.yaml")


# Periodic agents the scaffold enables on a brand-new repo, written as
# name-only presence files into its initial commit (see
# _write_periodic_presence_files). Enablement is per-repo file presence
# (.robotsix-mill/periodic/<name>.yaml) — NOT a repos.yaml block, which
# the loader no longer reads.
_DEFAULT_PERIODIC_NAMES = ("audit", "health")


def _append_repo_config(repo_info: RepoInfo, params: dict, settings) -> None:
    """Append a :class:`RepoConfig` stanza to ``config/repos.yaml`` for
    the newly created repository."""
    path = _repos_yaml_path()
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

    # Copy Langfuse keys from the robotsix-mill repo config
    mill_repo_id = settings.trace_review_target_repo_id
    if mill_repo_id:
        try:
            mill_repo_config = get_repos_config().repos[mill_repo_id]
            lf_public_key = mill_repo_config.langfuse_public_key
            lf_secret_key = mill_repo_config.langfuse_secret_key
            lf_base_url = mill_repo_config.langfuse_base_url
        except KeyError:
            log.warning(
                "trace_review_target_repo_id=%r not found in repos config; "
                "using empty Langfuse keys for new repo %r",
                mill_repo_id,
                repo_id,
            )
            lf_public_key = ""
            lf_secret_key = ""
            lf_base_url = "https://cloud.langfuse.com"
    else:
        lf_public_key = ""
        lf_secret_key = ""
        lf_base_url = "https://cloud.langfuse.com"

    language = params.get("language", "python")

    new_entry: dict = {
        "board_id": repo_id,
        "langfuse": {
            "project_name": repo_id,
            "public_key": lf_public_key,
            "secret_key": lf_secret_key,
            "base_url": lf_base_url,
        },
        "forge_remote_url": repo_info.clone_url,
        "language": language,
        # Periodic agents are NOT configured here — enablement is per-repo
        # file presence in the new repo's .robotsix-mill/periodic/ (written
        # into the initial scaffold commit). A repos.yaml "periodic" block is
        # dead (the loader ignores it).
        "test_command": "pytest -q" if language == "python" else "",
    }

    data["repos"][repo_id] = new_entry

    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)

    _reset_repos_config()
    log.info("Appended repo %r to %s", repo_id, path)


def _post_blocked_comment(ctx, ticket, body: str) -> None:
    """Post a comment on *ticket* and return nothing."""
    try:
        ctx.service.add_comment(
            ticket_id=ticket.id,
            body=body,
            author="robotsix-mill",
        )
    except Exception:
        log.exception("Failed to post blocked comment on ticket %s", ticket.id)
