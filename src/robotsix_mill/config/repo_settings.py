"""Per-repo settings file resolution.

A managed repo can own a handful of mill settings by committing a
per-repo settings file to its own source tree at:

    <repo_root>/.robotsix-mill/config.yaml

This file mirrors the per-repo keys of the fleet operator's central
``repos.yaml`` — a managed repo declares the value in its own source
tree instead of requiring an edit to the operator's central config.
It carries ``test_command`` (the shell command the implement test-gate
runs in the sandbox), ``languages`` (the programming language(s) the
repo uses, which drive per-language instruction injection into the
implement + refine agents), ``skip_ci`` (a boolean that opts the repo
out of forge-CI gating, ci_fix, and the periodic CI monitor),
``extra_sandbox_packages``, ``smoke_command``, and ``smoke_paths``;
the file is named generically so it can be extended with other per-repo
settings later.

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

log = logging.getLogger("robotsix_mill.config.repo_settings")


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
    raw = _load_repo_config_dict(repo_dir)
    if raw is None or "test_command" not in raw:
        return None
    value = raw["test_command"]
    if not isinstance(value, str):
        log.warning("repo settings: 'test_command' must be a string — ignoring")
        return None
    stripped = value.strip()
    return stripped or None


def load_repo_smoke_command(repo_dir: Path | None) -> str | None:
    """Return the per-repo ``smoke_command`` from
    ``<repo_dir>/.robotsix-mill/config.yaml``, or ``None``.

    Exact analogue of :func:`load_repo_test_command`: returns the
    stripped command when the file's top-level ``smoke_command`` key is
    present and is a non-empty string; otherwise returns ``None``. Never
    raises — a missing or malformed file is treated as "not set" so a
    managed repo can't take mill down by committing a broken file:

    * ``repo_dir is None`` → ``None``.
    * file absent → ``None`` (silent no-op).
    * unreadable / invalid YAML → ``log.warning`` + ``None``.
    * top-level not a mapping, or ``smoke_command`` value not a string
      → ``log.warning`` + ``None`` (clear type mismatch).
    * key absent, or value empty/whitespace-only → ``None`` (plain
      absence, no warning).
    """
    raw = _load_repo_config_dict(repo_dir)
    if raw is None or "smoke_command" not in raw:
        return None
    value = raw["smoke_command"]
    if not isinstance(value, str):
        log.warning("repo settings: 'smoke_command' must be a string — ignoring")
        return None
    stripped = value.strip()
    return stripped or None


def load_repo_smoke_paths(repo_dir: Path | None) -> list[str]:
    """Return the per-repo ``smoke_paths`` glob list from
    ``<repo_dir>/.robotsix-mill/config.yaml``, or ``[]``.

    Accepts ``smoke_paths: [src/runtime/**, src/x/*.css]`` (a list of
    glob strings). Strips whitespace from each entry and filters out
    empty/whitespace-only strings. Non-string items are coerced via
    ``str(x).strip()`` (matching :func:`load_repo_languages`). Never
    raises — a malformed value or missing file yields ``[]``.

    The globs scope the path-scoped smoke gate: an empty/absent list
    means the smoke command runs unconditionally (when set), otherwise
    the gate runs only when a ticket's introduced files match a glob.

    **Important:** ``smoke_paths`` is a smoke-test gating mechanism
    ONLY — it does NOT restrict which paths agent-stage filesystem
    tools (read_file, list_dir, run_command) can access.  The agent
    sandbox always mounts the full repo checkout; the globs here only
    decide whether the post-implement smoke command fires."""
    raw = _load_repo_config_dict(repo_dir)
    if raw is None:
        return []
    val = raw.get("smoke_paths")
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if "smoke_paths" in raw:
        log.warning("repo settings: 'smoke_paths' must be a list — ignoring")
    return []


def load_repo_skip_ci(repo_dir: Path | None) -> bool:
    """Return the per-repo ``skip_ci`` flag from
    ``<repo_dir>/.robotsix-mill/config.yaml``, or ``False``.

    Never raises — a missing or malformed file is treated as "not set"
    so a managed repo can't take mill down by committing a broken file:

    * ``repo_dir is None`` → ``False``.
    * file absent → ``False`` (silent no-op).
    * unreadable / invalid YAML → ``log.warning`` + ``False``.
    * top-level not a mapping → ``log.warning`` + ``False``.
    * ``skip_ci`` value present but not a bool → ``log.warning`` + ``False``.
    * key absent → ``False`` (plain absence, no warning).
    """
    raw = _load_repo_config_dict(repo_dir)
    if raw is None or "skip_ci" not in raw:
        return False
    value = raw["skip_ci"]
    if not isinstance(value, bool):
        log.warning("repo settings: 'skip_ci' must be a bool — ignoring")
        return False
    return value


def _load_repo_config_dict(repo_dir: Path | None) -> dict | None:
    """Read + validate ``<repo_dir>/.robotsix-mill/config.yaml`` into a dict.

    Shared hardened reader: returns the parsed top-level mapping, or
    ``None`` when the dir is absent / file missing / unreadable / invalid
    YAML / not a mapping. Never raises (a managed repo must not be able to
    crash mill by committing a broken file)."""
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
    return raw


def load_repo_languages(repo_dir: Path | None) -> list[str]:
    """Return the programming language(s) the repo declares in
    ``.robotsix-mill/config.yaml``, or ``[]``.

    Accepts either ``languages: [python, rust]`` (a list) or the singular
    ``language: python`` (a string), normalising both to a list of
    non-empty, stripped strings. ``languages`` takes precedence when both
    are present. Never raises — a malformed value yields ``[]``."""
    raw = _load_repo_config_dict(repo_dir)
    if raw is None:
        return []
    val = raw.get("languages")
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    single = raw.get("language")
    if isinstance(single, str) and single.strip():
        return [single.strip()]
    return []


def load_extra_sandbox_packages(repo_dir: Path | None) -> list[str]:
    """Return the extra sandbox packages the repo declares in
    ``.robotsix-mill/config.yaml``, or ``[]``.

    Accepts ``extra_sandbox_packages: [colcon, ros-humble-ros-core]``
    (a list). Strips whitespace from each entry and filters out
    empty/whitespace-only strings. Non-string items are coerced via
    ``str(x).strip()`` (matching ``load_repo_languages``). Never raises
    — a malformed value or missing file yields ``[]``.

    Entry grammar (implemented in ``sandbox._build_extra_packages_prefix``):

    * ``pip:<name>`` → installed via ``pip install --user`` (Python
      packages, e.g. ``pip:yamllint``).
    * ``apt:<name>`` → installed via ``apt-get install -y`` (system
      tools, e.g. ``apt:shellcheck``).
    * a bare ``<name>`` (no prefix) defaults to apt — the sandbox image
      is Debian-based.

    Declaring any apt package (prefixed or bare) makes the sandbox drop
    ``--read-only`` and add tmpfs mounts for apt's state directories so
    the install can write to the root filesystem; pip-only package sets
    keep the read-only root."""
    raw = _load_repo_config_dict(repo_dir)
    if raw is None:
        return []
    val = raw.get("extra_sandbox_packages")
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if "extra_sandbox_packages" in raw:
        log.warning("repo settings: 'extra_sandbox_packages' must be a list — ignoring")
    return []


def warn_if_deprecated_log_folder(repo_dir: Path | None) -> None:
    """Log a deprecation warning if a managed repo still commits the
    repo-owned ``deployed_log_folder`` key in ``.robotsix-mill/config.yaml``.

    The key is no longer read from the managed repo — ``deployed_log_folder``
    is now an operator-controlled per-repo key in mill's central
    ``config/repos.yaml`` (it is a deployment-specific host path that must
    not be committed into the managed repo). This helper only nudges the
    operator to migrate; it never raises and returns nothing."""
    raw = _load_repo_config_dict(repo_dir)
    if raw is not None and "deployed_log_folder" in raw:
        log.warning(
            "repo settings: 'deployed_log_folder' in .robotsix-mill/config.yaml "
            "is deprecated and ignored — set it per-repo in mill's "
            "config/repos.yaml instead"
        )


def _load_language_snippet(settings, repo_dir: Path | None, lang: str) -> str:
    """Resolve the instruction snippet for one language, repo override
    first then the mill's built-in library. Returns ``""`` if neither
    exists."""
    if repo_dir is not None:
        override = (
            Path(repo_dir) / ".robotsix-mill" / "language_instructions" / f"{lang}.md"
        )
        try:
            if override.is_file():
                return override.read_text(encoding="utf-8")
        except OSError:
            pass
    from robotsix_mill._resources import effective_language_instructions_dir

    builtin = (
        effective_language_instructions_dir(settings.language_instructions_dir)
        / f"{lang}.md"
    )
    try:
        return builtin.read_text(encoding="utf-8")
    except OSError:
        log.info(
            "language %r declared but no snippet (repo override or built-in %s) "
            "— skipping",
            lang,
            builtin,
        )
        return ""


def resolve_language_instructions(settings, repo_dir) -> str:
    """Resolve the concatenated language-instruction block for a repo.

    The language(s) come solely from the repo's own
    ``.robotsix-mill/config.yaml`` (``languages``/``language``) — a managed
    repo owns its language declaration (the old ``repos.yaml`` ``language``
    fallback was removed).

    Per language, the snippet source is: the repo's
    ``.robotsix-mill/language_instructions/<lang>.md`` (house override) if
    present, else the mill's built-in
    ``agent_definitions/language_instructions/<lang>.md``. Snippets for
    multiple languages are concatenated. Returns ``""`` when no language is
    declared or no snippets resolve."""
    langs = load_repo_languages(repo_dir)
    blocks = [
        text.strip()
        for lang in langs
        if (text := _load_language_snippet(settings, repo_dir, lang)).strip()
    ]
    return "\n\n".join(blocks)
