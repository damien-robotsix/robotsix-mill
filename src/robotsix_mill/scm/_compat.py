"""Backward-compat aliasing for packages relocated under ``robotsix_mill.scm``.

``alias_package(old, new)`` makes ``old`` and every ``old.<sub>`` resolve to
the *same* module objects as ``new`` / ``new.<sub>`` — so attribute access and
``monkeypatch.setattr("old.x.y", ...)`` keep working without loading a second
copy of any module.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from types import ModuleType


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self._target = target

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module = importlib.import_module(self._target)
        self._spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Already executed under its canonical name; the import machinery
        # unconditionally overwrote ``__spec__`` with the alias spec — restore it.
        module.__spec__ = self._spec


class _AliasFinder(importlib.abc.MetaPathFinder):
    def __init__(self, old: str, new: str) -> None:
        self._prefix = old + "."
        self._new = new

    def find_spec(self, fullname, path=None, target=None):  # type: ignore[no-untyped-def]
        if not fullname.startswith(self._prefix):
            return None
        canonical = self._new + fullname[len(self._prefix) - 1 :]
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(canonical))


def alias_package(old: str, new: str) -> None:
    """Point ``old`` (and its submodules) at the relocated package ``new``."""
    if not any(
        isinstance(f, _AliasFinder) and f._prefix == old + "." for f in sys.meta_path
    ):
        sys.meta_path.insert(0, _AliasFinder(old, new))
    sys.modules[old] = importlib.import_module(new)
