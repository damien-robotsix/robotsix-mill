"""Recursive schema-validation for every agent definition YAML.

Discovers every ``agent_definitions/**/*.yaml`` (top-level, ``periodic/``,
and ``pipeline/``) and validates each one against the ``AgentDefinition``
Pydantic schema via ``load_agent_definition()``. This closes the gap left
by the non-recursive globs in ``test_yaml_loader.py`` /
``test_build_agent_from_definition.py``, which never validated the files
under the subdirectories.

No env-var mocking is needed: ``load_agent_definition()`` resolves unset
``${VAR}`` model references to ``""``, which still satisfies the
``model: str`` field.
"""

import importlib
from pathlib import Path

import pytest

from robotsix_mill.agents.yaml_loader import (
    AgentDefinition,
    load_agent_definition,
)

_ALL_DEFINITIONS = sorted(
    p
    for p in Path("agent_definitions").rglob("*.yaml")
    if "agent_definitions/_shared/" not in str(p)
)


def _definitions_with_output_type():
    """Yield (path, definition) for every definition with a non-empty output_type."""
    result = []
    for p in _ALL_DEFINITIONS:
        ad = load_agent_definition(p)
        if ad.output_type and ad.output_type.strip():
            result.append((p, ad))
    return result


@pytest.mark.parametrize("path", _ALL_DEFINITIONS, ids=str)
def test_agent_definition_validates(path):
    """Every agent definition parses into a valid AgentDefinition."""
    ad = load_agent_definition(path)
    assert isinstance(ad, AgentDefinition)
    assert ad.name, f"{path} has empty name"


def test_discovery_is_recursive():
    """Guard against a regression to a non-recursive glob.

    If discovery ever drops back to ``glob('*.yaml')`` the ``periodic``
    and ``pipeline`` subdirectories would disappear from the set and this
    test fails loudly.
    """
    assert _ALL_DEFINITIONS, "No agent definition YAMLs discovered"
    parents = {p.parent.name for p in _ALL_DEFINITIONS}
    assert "periodic" in parents, f"'periodic' not in discovered parents: {parents}"
    assert "pipeline" in parents, f"'pipeline' not in discovered parents: {parents}"


@pytest.mark.parametrize(
    "path, definition",
    _definitions_with_output_type(),
    ids=lambda p: str(p),
)
def test_output_type_symbol_resolves(path, definition):
    """Every definition's output_type symbol is importable from its module.

    Mirrors the import path heuristic in ``build_agent_from_definition``
    (base.py:187-192): a dotted ``module`` is resolved under the package
    root (``robotsix_mill.<module>``); a dotless one under the agents
    package (``robotsix_mill.agents.<module>``).  An AttributeError here
    is the same crash that hits production when the agent is built.
    """
    assert definition.module and definition.module.strip(), (
        f"{path}: output_type='{definition.output_type}' but module is None/empty"
    )
    if "." in definition.module:
        module = importlib.import_module(f"robotsix_mill.{definition.module}")
    else:
        module = importlib.import_module(f"robotsix_mill.agents.{definition.module}")
    output_cls = getattr(module, definition.output_type)
    assert output_cls is not None, (
        f"{path}: output_type={definition.output_type!r} resolved to None"
    )


def test_bogus_output_type_raises_attributeerror():
    """A typo'd output_type fails with AttributeError — the exact crash this
    test suite is designed to catch before deployment."""
    ad = load_agent_definition(Path("agent_definitions/refine.yaml"))
    assert ad.output_type and ad.module
    module = importlib.import_module(f"robotsix_mill.agents.{ad.module}")
    with pytest.raises(AttributeError):
        getattr(module, "NoSuchResultTypo123")  # noqa: B009 — mirrors base.py:193
