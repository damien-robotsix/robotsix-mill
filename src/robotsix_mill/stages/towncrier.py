"""Generate towncrier changelog fragments when the target repo requires them.

When a repo enforces changelog fragments via ``[tool.towncrier]`` in
``pyproject.toml``, CI fails on any PR that doesn't add a fragment file.
This module generates a trivial ``.misc.md`` fragment so the implement
stage doesn't leave that cleanup to the fixing_ci stage — saving ~48–162s
and ~$0.01–0.02 per affected ticket.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("robotsix_mill.towncrier")


def maybe_generate_towncrier_fragment(
    repo_dir: Path,
    ticket_id: str,
    title: str,
) -> bool:
    """Create a towncrier ``.misc.md`` fragment in *repo_dir* if the repo
    enforces changelogs via ``[tool.towncrier]`` in ``pyproject.toml``.

    Returns ``True`` when a fragment file was created, ``False``
    otherwise (no ``pyproject.toml``, no towncrier config, or any error).
    Never raises — a broken/malicious ``pyproject.toml`` must not crash
    the stage.
    """
    pp = repo_dir / "pyproject.toml"
    if not pp.is_file():
        return False

    try:
        import tomllib

        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:
        log.warning(
            "towncrier: failed to parse %s — skipping fragment generation",
            pp,
            exc_info=True,
        )
        return False

    tc = (data.get("tool", {}) or {}).get("towncrier")
    if not tc:
        return False

    from robotsix_mill.agents.changelog_tool import _DEFAULT_FRAGMENT_DIR

    directory = str(tc.get("directory") or _DEFAULT_FRAGMENT_DIR)

    try:
        fragment_dir = repo_dir / directory
        fragment_dir.mkdir(parents=True, exist_ok=True)

        # Slugify the ticket id the same way _add_changelog_fragment
        # does, so the glob check finds fragments the agent wrote.
        from robotsix_mill.agents.changelog_tool import _slugify_ticket

        slug = _slugify_ticket(ticket_id)
        existing = (
            list(fragment_dir.glob(f"{slug}.*.md")) if fragment_dir.is_dir() else []
        )
        if existing:
            log.info(
                "towncrier: fragment %s already exists for %s — skipping .misc.md",
                existing[0].name,
                ticket_id,
            )
            return False

        fragment_file = fragment_dir / f"{slug}.misc.md"
        fragment_file.write_text(title + "\n", encoding="utf-8")
    except OSError:
        log.warning(
            "towncrier: failed to write fragment for ticket %s",
            ticket_id,
            exc_info=True,
        )
        return False

    log.info(
        "towncrier: created fragment %s",
        fragment_file,
    )
    return True
