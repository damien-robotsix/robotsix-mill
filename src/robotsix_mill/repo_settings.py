"""Per-repo settings file resolution.

A managed repo can own a handful of mill settings by committing a
per-repo settings file to its own source tree at:

    <repo_root>/.robotsix-mill/config.yaml

This file mirrors the per-repo keys of the fleet operator's central
``repos.yaml`` — a managed repo declares the value in its own source
tree instead of requiring an edit to the operator's central config.
It currently carries only ``test_command`` (the shell command the
implement test-gate runs in the sandbox); the file is named
generically so it can be extended with other per-repo settings later.

Every loader here follows the same hardening contract used by
``agents/periodic_loader.py`` and ``agents/overlays.py``: a managed
repo MUST NOT be able to crash mill by committing a broken file, so a
malformed/missing file is a silent no-op (or a logged warning) that
returns ``None`` rather than raising.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger("robotsix_mill.repo_settings")


def load_repo_test_command(repo_dir: Path | None) -> str | None:
    """Return the per-repo ``test_command`` from
    ``<repo_dir>/.robotsix-mill/config.yaml``, or ``None``.

    Returns the stripped command when the file's top-level
    ``test_command`` key is present and is a non-empty string;
    otherwise returns ``None``. Never raises — a missing or malformed
    file is treated as "not set" so a managed repo can't take mill
    down by committing a broken file:

    * ``repo_dir is None`` → ``None``.
    * file absent → ``None`` (silent no-op).
    * unreadable / invalid YAML → ``log.warning`` + ``None``.
    * top-level not a mapping, or ``test_command`` value not a string
      → ``log.warning`` + ``None`` (clear type mismatch).
    * key absent, or value empty/whitespace-only → ``None`` (plain
      absence, no warning).
    """
    if repo_dir is None:
        return None
    path = Path(repo_dir) / ".robotsix-mill" / "config.yaml"
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("repo settings %s: read/parse error — ignoring (%s)", path, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("repo settings %s: top-level must be a mapping — ignoring", path)
        return None
    if "test_command" not in raw:
        return None
    value = raw["test_command"]
    if not isinstance(value, str):
        log.warning(
            "repo settings %s: 'test_command' must be a string — ignoring", path
        )
        return None
    stripped = value.strip()
    return stripped or None
