#!/usr/bin/env python3
"""Generate shell completion scripts for ``robotsix-mill`` via shtab.

Produces static bash and zsh completion scripts by introspecting
the argparse parser built by ``robotsix_mill.cli.build_parser()``.
(fish is not yet supported by the current shtab release; add it when
a version with fish support lands on PyPI.)

Usage::

    python scripts/gen_completions.py

Output lands in ``contrib/completions/``, which is force-included in the
wheel at ``robotsix_mill/completions/``.
"""

from __future__ import annotations

import os
import sys

# Ensure the src/ tree is importable, whether this script is invoked
# directly or via ``python scripts/gen_completions.py``.
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_repo_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import shtab
from robotsix_mill.cli._parser import build_parser

OUT_DIR = os.path.join(_repo_root, "contrib", "completions")
SHELLS = ("bash", "zsh")


def main() -> None:
    parser = build_parser()
    os.makedirs(OUT_DIR, exist_ok=True)

    for shell in SHELLS:
        path = os.path.join(OUT_DIR, f"robotsix-mill.{shell}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(shtab.complete(parser, shell=shell))
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
