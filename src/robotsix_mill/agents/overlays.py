"""Per-repo prompt overlays for generic periodic agents.

A managed repo can carry repo-specific prompt guidance for any of
mill's generic periodic agents in its own source tree, under:

    <repo_root>/.robotsix-mill/agent_overlays/<agent_name>.md

When mill clones the repo for a periodic pass, the overlay file (if
present) is appended to the agent's shipped system prompt. The
managed repo therefore owns its specialisation: clones, forks, and
fresh mill deployments pick it up automatically.

Mill itself is one of the managed repos and ships its own overlays
git-tracked at the same path in its source tree.
"""

from __future__ import annotations

from pathlib import Path


def load_overlay(repo_dir: Path | None, agent_name: str) -> str:
    """Return the per-repo overlay Markdown for *agent_name*, or ``""``.

    Looks at ``<repo_dir>/.robotsix-mill/agent_overlays/<agent_name>.md``.
    Returns the file's contents stripped of surrounding whitespace.
    Returns ``""`` when ``repo_dir`` is ``None`` (no clone available),
    the directory does not exist, or the file is absent — every
    branch must be a silent no-op so a repo with no `.robotsix-mill/`
    folder behaves exactly as before.
    """
    if repo_dir is None:
        return ""
    path = Path(repo_dir) / ".robotsix-mill" / "agent_overlays" / f"{agent_name}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def apply_overlay(system_prompt: str, overlay: str) -> str:
    """Append *overlay* to *system_prompt* with one blank line separator.

    Empty overlay → system_prompt unchanged. Keeps the caller from
    having to know the formatting convention.
    """
    if not overlay:
        return system_prompt
    return f"{system_prompt}\n\n{overlay}\n"
