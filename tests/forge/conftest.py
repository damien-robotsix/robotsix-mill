"""Make the ``robotsix-github-auth`` dependency importable during tests.

``robotsix-github-auth`` is a git dependency resolved by ``uv sync --locked``
via ``[tool.uv.sources]``.  ``pip``-based installs ignore that table, so a
hermetic sandbox that cannot fetch git sources has no installed copy.  When
the real package is absent, fall back to the vendored copy under
``tests/forge/_vendor/`` so test collection still succeeds.  CI (which runs
``uv sync --locked``) keeps using the genuine locked dependency — the
vendored copy is only a fallback.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("robotsix_github_auth") is None:
    _VENDOR = Path(__file__).parent / "_vendor"
    if _VENDOR.is_dir():
        sys.path.insert(0, str(_VENDOR))
