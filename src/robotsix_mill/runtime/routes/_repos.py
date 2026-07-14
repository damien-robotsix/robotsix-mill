"""Runtime repo registration — write the machine-owned overlay.

POST /repos writes a new :class:`RepoConfig` entry to
``<data_dir>/registered_repos.yaml`` under the ``repos:`` key and
hot-reloads the in-process ``ReposRegistry`` so the new repo is
immediately visible without a container restart.

Re-registering an existing ``repo_id`` is idempotent — returns 200
and does not touch the overlay file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator

from ...config import RepoConfig, ReposRegistry, Settings
from ...config.repos import _reset_repos_config, load_repos_config
from ..deps import get_repos_registry, get_settings

router = APIRouter(tags=["Repos"])

log = logging.getLogger(__name__)


def _sanitize_log_value(value: str) -> str:
    """Replace newlines to prevent log-forging attacks."""
    return value.replace("\n", " ").replace("\r", " ")


class RepoRegistration(BaseModel):
    """Request body for POST /repos."""

    repo_id: str
    forge_remote_url: str
    board_id: str | None = None  # defaults to repo_id

    @field_validator("repo_id")
    @classmethod
    def _validate_repo_id(cls, v: str) -> str:
        if any(c in v for c in "\n\r\0"):
            raise ValueError("repo_id must not contain newlines or null bytes")
        return v

    @field_validator("forge_remote_url")
    @classmethod
    def _validate_credential_free_url(cls, v: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(v)
        if parts.hostname is not None and (parts.username or parts.password):
            raise ValueError(
                "forge_remote_url must not contain credentials "
                "(userinfo like 'token@host' or 'user:pass@host')"
            )
        return v

    @field_validator("board_id")
    @classmethod
    def _validate_board_id(cls, v: str | None) -> str | None:
        if v is not None and any(c in v for c in "\n\r\0"):
            raise ValueError("board_id must not contain newlines or null bytes")
        return v


class RepoRegistrationResult(BaseModel):
    """Response body for POST /repos."""

    repo_id: str
    board_id: str
    forge_remote_url: str | None
    registered: bool  # True = newly written, False = already existed


@router.post(
    "/repos",
    status_code=status.HTTP_201_CREATED,
    response_model=RepoRegistrationResult,
)
def register_repo(
    body: RepoRegistration,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    repos: ReposRegistry = Depends(get_repos_registry),
) -> RepoRegistrationResult:
    """Register a repository at runtime by writing its entry to the
    machine-owned overlay (``registered_repos.yaml``) and hot-reloading
    the in-process :class:`ReposRegistry`.

    Idempotent: re-registering an existing ``repo_id`` returns 200 with
    the effective entry and does not touch the overlay file — operator
    config entries are never modified.

    New registrations return 201 and the repo is immediately visible via
    ``request.app.state.repos`` without a container restart.
    """
    # Gate: runtime repo registration is opt-in. When the flag is off
    # (default), only operator-configured repos (in config/config.json)
    # are accepted — POST /repos is refused.
    if not settings.allow_runtime_repo_registration:
        raise HTTPException(
            status_code=403,
            detail="Runtime repo registration is disabled. "
            "Set allow_runtime_repo_registration=true in config to enable.",
        )

    effective_board_id = body.board_id or body.repo_id

    # Idempotency: if the repo_id already exists in the registry,
    # return the existing entry with no file I/O.  This also guards
    # operator-configured repos — they win on conflict (operator config
    # takes priority over the overlay in _load_repos_document), so we
    # must never write an overlay entry that would be shadowed.
    if body.repo_id in repos.repos:
        existing = repos.repos[body.repo_id]
        response.status_code = status.HTTP_200_OK
        return RepoRegistrationResult(
            repo_id=body.repo_id,
            board_id=existing.board_id,
            forge_remote_url=existing.forge_remote_url,
            registered=False,
        )

    # Write the overlay YAML.
    # Resolve and validate the overlay path: realpath + containment
    # check prevents path traversal.  Both os.path.realpath calls are
    # recognized as sanitizers by CodeQL's py/path-injection query.
    _data_root = os.path.realpath(os.fspath(settings.data_dir))
    _safe_path_str = os.path.realpath(os.path.join(_data_root, "registered_repos.yaml"))
    # The joined leaf is a constant filename, so the resolved path always
    # ends with "/<leaf>" under the root — a plain prefix check is exact.
    if not _safe_path_str.startswith(_data_root + os.sep):
        raise ValueError(
            f"Path escapes data directory: {_safe_path_str} is not within {_data_root}"
        )
    _safe_path = Path(_safe_path_str)
    if os.path.exists(_safe_path):
        with open(_safe_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    else:
        data = {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("repos", {})

    data["repos"][body.repo_id] = {
        "board_id": effective_board_id,
        "forge_remote_url": body.forge_remote_url,
        "_mill_source": "auto",
    }

    Path(_safe_path).parent.mkdir(parents=True, exist_ok=True)
    with open(_safe_path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)

    # Hot-reload: clear the cached singleton, then re-read the merged
    # config and store the fresh registry on app state so in-flight
    # requests (and the worker) see the new repo without a restart.
    #
    # We call load_repos_config() first — in the normal deployment case
    # (where data_dir matches the YAML config's service.data_dir) this
    # picks up the overlay we just wrote.  Then we ensure the new repo
    # is present (in case load_repos_config didn't find the overlay,
    # e.g. the test suite patches Settings.data_dir independently).
    # Finally, we add any operator-configured repos that were in the
    # original registry but not in the reloaded result, preserving the
    # full set.
    #
    # NOTE: this mutates app.state.repos non-atomically. Concurrent
    # /repos registrations during the reload window are an accepted
    # race — the last writer wins, and the overlay file write + reload
    # cycle is idempotent (same repo_id overwrites itself).
    _reset_repos_config()
    new_repos = load_repos_config()
    # Ensure the new repo is present even when load_repos_config didn't
    # pick up the overlay (data_dir mismatch, empty repos file, etc.).
    if body.repo_id not in new_repos.repos:
        new_repos.repos[body.repo_id] = RepoConfig(
            repo_id=body.repo_id,
            board_id=effective_board_id,
            forge_remote_url=body.forge_remote_url,
            langfuse_project_name="",
            langfuse_public_key="",
            langfuse_secret_key="",
            source="auto",
        )
    # Preserve operator-configured repos that load_repos_config may have
    # missed (e.g. when MILL_REPOS_FILE="" blocks all config reading in
    # the test suite).
    for rid, rc in repos.repos.items():
        if rid not in new_repos.repos:
            new_repos.repos[rid] = rc
    request.app.state.repos = new_repos

    log.info(
        "Registered repo %r (board %r) via runtime overlay",
        _sanitize_log_value(body.repo_id),
        _sanitize_log_value(effective_board_id),
    )

    return RepoRegistrationResult(
        repo_id=body.repo_id,
        board_id=effective_board_id,
        forge_remote_url=body.forge_remote_url,
        registered=True,
    )


@router.delete(
    "/repos/{repo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def deregister_repo(
    repo_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    repos: ReposRegistry = Depends(get_repos_registry),
) -> None:
    """Remove a runtime-registered repo from the machine-owned overlay.

    Only repos with ``source="auto"`` (machine-registered) can be
    deregistered.  Operator-configured repos (``source="config"``)
    are permanent and return 403.  Unknown repos return 404.

    After removal the overlay YAML is updated and the in-process
    :class:`ReposRegistry` is hot-reloaded.
    """
    if repo_id not in repos.repos:
        raise HTTPException(status_code=404, detail=f"Unknown repo_id: {repo_id!r}")

    rc = repos.repos[repo_id]
    if rc.source != "auto":
        raise HTTPException(
            status_code=403,
            detail=f"Repo '{repo_id}' is operator-configured and cannot be "
            "deregistered via API. Remove it from config/config.json instead.",
        )

    # Remove from the overlay YAML file.
    _data_root = os.path.realpath(os.fspath(settings.data_dir))
    _safe_path_str = os.path.realpath(os.path.join(_data_root, "registered_repos.yaml"))
    if not _safe_path_str.startswith(_data_root + os.sep):
        raise ValueError(
            f"Path escapes data directory: {_safe_path_str} is not within {_data_root}"
        )
    overlay_path = Path(_safe_path_str)
    if overlay_path.exists():
        data = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and "repos" in data:
            data["repos"].pop(repo_id, None)
            if not data["repos"]:
                data.pop("repos", None)
            with open(overlay_path, "w", encoding="utf-8") as fh:
                yaml.dump(data, fh, default_flow_style=False, sort_keys=False)

    # Hot-reload: clear cached singleton and re-read merged config.
    _reset_repos_config()
    new_repos = load_repos_config()
    # Preserve operator-configured repos from the original registry
    # that load_repos_config may have missed.
    for rid, r_config in repos.repos.items():
        if rid != repo_id and rid not in new_repos.repos:
            new_repos.repos[rid] = r_config
    request.app.state.repos = new_repos

    log.info(
        "Deregistered repo %r via runtime overlay",
        _sanitize_log_value(repo_id),
    )
