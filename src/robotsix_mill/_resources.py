"""Locate bundled data directories in both editable and installed modes."""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Paths already warned about by the effective_* fallbacks — warn once per
# configured path per process instead of on every prompt composition.
_warned_missing: set[tuple[str, str]] = set()


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


def _effective_dir(name: str, configured: Path, packaged: Path) -> Path:
    """*configured* if it exists, else the *packaged* copy (warn once).

    A CWD-relative override (e.g. ``skills_dir: skills``, written for an
    editable checkout where CWD is the repo root) resolves against ``/app``
    inside the container and doesn't exist there — before this fallback the
    implement preflight then hard-blocked EVERY ticket with "missing skill
    file", including the ticket that would have fixed the configuration.
    Resolved at use time (not Settings construction) so a directory created
    after startup is still honored.
    """
    if configured.is_dir() or configured == packaged or not packaged.is_dir():
        return configured
    key = (name, str(configured))
    if key not in _warned_missing:
        _warned_missing.add(key)
        log.warning(
            "%s %r does not exist (CWD-relative override?) — "
            "falling back to packaged %s",
            name,
            str(configured),
            packaged,
        )
    return packaged


def effective_skills_dir(configured: Path) -> Path:
    """The skills directory to actually read from: *configured* if it
    exists, else the packaged ``skills/`` copy."""
    return _effective_dir("skills_dir", configured, skills_dir())


def effective_language_instructions_dir(configured: Path) -> Path:
    """The language-instructions directory to actually read from:
    *configured* if it exists, else the packaged copy."""
    return _effective_dir(
        "language_instructions_dir", configured, language_instructions_dir()
    )
