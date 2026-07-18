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
