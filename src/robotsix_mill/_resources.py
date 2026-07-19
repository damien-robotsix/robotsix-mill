"""Locate bundled data directories in both editable and installed modes."""

from __future__ import annotations

import importlib.resources
from pathlib import Path


def _resource_dir(name: str) -> Path:
    # In an installed wheel, agent_definitions/ and expert_definitions/ are
    # bundled inside the robotsix_mill package via hatch force-include, so
    # importlib.resources.files() finds them directly.
    # In an editable install they live at the repo root: three parents above
    # this file (src/robotsix_mill/_resources.py -> src/robotsix_mill/ ->
    # src/ -> repo root).
    pkg_path = Path(str(importlib.resources.files("robotsix_mill"))) / name
    if pkg_path.is_dir():
        return pkg_path
    return Path(__file__).parent.parent.parent / name


def agent_definitions_dir() -> Path:
    """Return the path to the agent_definitions directory."""
    return _resource_dir("agent_definitions")


def expert_definitions_dir() -> Path:
    """Return the path to the expert_definitions directory."""
    return _resource_dir("expert_definitions")


def skills_dir() -> Path:
    """Return the path to the skills directory."""
    return _resource_dir("skills")


def language_instructions_dir() -> Path:
    """Return the path to the per-language instruction Markdown snippets.

    These live under ``agent_definitions/language_instructions/``,
    bundled inside the package in installed mode and at the repo root
    in editable mode.
    """
    return _resource_dir("agent_definitions") / "language_instructions"
