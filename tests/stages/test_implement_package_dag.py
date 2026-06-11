"""Structural guard: the ``stages/implement`` package is an acyclic DAG.

A prior split of the implement monolith introduced 11x
``py/unsafe-cyclic-import`` because the submodules cross-imported each
other. This test pins the import shape so the cycle cannot regress:

- ``_shared`` is a pure leaf — it imports no sibling submodule.
- No mixin submodule imports another mixin submodule or ``core``.
- Only ``__init__`` (the façade) imports ``core``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import robotsix_mill.stages.implement as impl_pkg

PKG_DIR = Path(impl_pkg.__file__).parent
MIXINS = {
    "phase_coordinator",
    "validation",
    "implementation_logic",
    "file_operations",
}


def _relative_sibling_imports(path: Path) -> set[str]:
    """Return the set of sibling submodule names imported by *path*.

    Covers both ``from .sibling import x`` and ``from . import sibling``
    forms (level == 1, i.e. a same-package relative import).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    siblings: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            # ``from .sibling import ...`` -> module == "sibling"
            siblings.add(node.module.split(".")[0])
        else:
            # ``from . import sibling, other``
            for alias in node.names:
                siblings.add(alias.name)
    return siblings


def test_shared_is_a_pure_leaf() -> None:
    """``_shared`` imports no sibling submodule (one-way leaf)."""
    siblings = _relative_sibling_imports(PKG_DIR / "_shared.py")
    assert siblings == set(), f"_shared.py must be a leaf, imports: {siblings}"


def test_no_mixin_imports_a_sibling_mixin_or_core() -> None:
    """No mixin imports another mixin submodule or ``core``."""
    for mixin in MIXINS:
        siblings = _relative_sibling_imports(PKG_DIR / f"{mixin}.py")
        forbidden = siblings & (MIXINS | {"core"})
        forbidden.discard(mixin)
        assert not forbidden, f"{mixin}.py introduces a cross-edge: {forbidden}"


def test_only_init_imports_core() -> None:
    """``core`` is imported only by the package façade (``__init__``)."""
    for py in PKG_DIR.glob("*.py"):
        if py.name in {"__init__.py", "core.py"}:
            continue
        assert "core" not in _relative_sibling_imports(py), (
            f"{py.name} must not import core"
        )
    assert "core" in _relative_sibling_imports(PKG_DIR / "__init__.py")
