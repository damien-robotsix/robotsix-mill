"""Resolver for bundled data directories shipped alongside the package.

The mill ships three repo-root data directories that are loaded at
runtime — ``agent_definitions/`` (agent + periodic + pipeline YAML and
language-instruction snippets), ``expert_definitions/`` (expert-domain
YAML), and ``skills/`` (skill docs).  They exist in two layouts:

* **dev / editable** — the directories sit at the repository root and the
  package is installed with ``pip install -e .`` (or the dev image's
  ``COPY . /app`` tree).  ``Path(__file__)`` points into ``src/`` and the
  repo root is three parents up.

* **installed (production wheel)** — hatch ``force-include`` copies each
  directory *inside* the installed package (``robotsix_mill/<name>``), so
  it lands next to this module in ``site-packages``.  The old
  ``Path(__file__).parent.parent.parent.parent`` chain resolved to
  ``/usr/local/lib/python3.14/`` there (wrong, and the files were never
  shipped), crash-looping ``robotsix-mill serve``.

:func:`data_dir` resolves the directory in both layouts: it prefers the
installed copy alongside the package and falls back to the repo root.
"""

from __future__ import annotations

from pathlib import Path


def data_dir(name: str) -> Path:
    """Resolve a bundled data directory in both installed and dev layouts.

    *name* is one of ``"agent_definitions"``, ``"expert_definitions"``,
    or ``"skills"``.  The installed copy (force-included alongside the
    package) wins when present; otherwise the repo-root directory (dev /
    editable checkout) is returned.
    """
    here = Path(__file__).resolve().parent  # …/robotsix_mill
    installed = here / name  # site-packages/robotsix_mill/<name> (production)
    if installed.exists():
        return installed
    # dev / editable: src/robotsix_mill/data_paths.py → repo root is 3 up.
    return here.parent.parent / name
