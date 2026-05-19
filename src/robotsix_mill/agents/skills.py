"""Skill loader.

A skill is ``skills/<name>/SKILL.md``: YAML-ish frontmatter
(``name`` / ``description`` / ``when_to_use``) + a Markdown how-to body.
Skills are *instructional* — they teach the agent to use existing tools
(``web_research``, ``explore``, ``run_tests``, …), they are not new code
paths. The loader concatenates them into a prompt section so the
refine/implement agents know what they can do and when.
"""

from __future__ import annotations

import re
from pathlib import Path

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_KEY = re.compile(r"^([A-Za-z_]+)\s*:\s*(.*)$")


def _parse(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text.strip()
    front, body = m.group(1), m.group(2)
    meta: dict[str, str] = {}
    key = None
    for line in front.splitlines():
        km = _KEY.match(line)
        if km:
            key = km.group(1).lower()
            meta[key] = km.group(2).strip().strip(">|").strip()
        elif key and line.strip():  # folded continuation
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, body.strip()


def load_skills(skills_dir: Path) -> str:
    """Return a system-prompt section describing every skill, or "" if
    there are none / the dir is missing."""
    d = Path(skills_dir)
    if not d.is_dir():
        return ""
    blocks: list[str] = []
    for sk in sorted(d.glob("*/SKILL.md")):
        try:
            meta, body = _parse(sk.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = meta.get("name") or sk.parent.name
        parts = [f"## Skill: {name}"]
        if meta.get("description"):
            parts.append(meta["description"])
        if meta.get("when_to_use"):
            parts.append(f"**When to use:** {meta['when_to_use']}")
        if body:
            parts.append(body)
        blocks.append("\n\n".join(parts))
    if not blocks:
        return ""
    return (
        "\n\n# Skills\n\nYou have the following skills. Consult the "
        "relevant one before acting.\n\n" + "\n\n---\n\n".join(blocks)
    )
