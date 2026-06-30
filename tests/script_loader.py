"""Canonical helper for loading files under ``scripts/`` in tests.

This is the one place tests should reach for when they need to import a
file from ``scripts/`` — including **extensionless** scripts (a bare
``scripts/<name>`` with no suffix). Python's ``importlib`` cannot infer
a ``SourceFileLoader`` from an empty file suffix, so loading an
extensionless file via a bare ``spec_from_file_location()`` (no
``loader=``) fails. Supplying an explicit
``importlib.machinery.SourceFileLoader`` makes the helper work
uniformly for extensionless files and ordinary ``.py`` files alike.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType


def load_script(script_path: Path, module_name: str | None = None) -> ModuleType:
    name = module_name or script_path.stem.replace("-", "_")
    loader = SourceFileLoader(name, str(script_path))
    spec = importlib.util.spec_from_file_location(name, script_path, loader=loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module
