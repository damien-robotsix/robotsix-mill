"""Tests for the bundled schema and the validation API."""

from __future__ import annotations

import yaml

from robotsix_modules import SCHEMA_PATH, validate
from robotsix_modules.schema import load_schema


def test_schema_path_resolves_and_is_readable() -> None:
    assert SCHEMA_PATH.is_file()
    assert SCHEMA_PATH.read_text(encoding="utf-8").strip()


def test_bundled_schema_parses_and_has_expected_keys() -> None:
    schema = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == (
        "https://robotsix.github.io/modules/schemas/modules.schema.yaml"
    )
    module = schema["$defs"]["module"]
    for field in ("id", "description", "paths", "dependencies"):
        assert field in module["properties"]


def test_load_schema_matches_path_contents() -> None:
    assert load_schema()["title"] == "Module Taxonomy Schema"


def test_empty_dict_reports_missing_modules() -> None:
    errors = validate({})
    assert errors
    assert any("required property" in e and "modules" in e for e in errors)


def test_valid_taxonomy_returns_empty_list() -> None:
    taxonomy = {
        "modules": [{"id": "foo", "description": "bar", "paths": ["src/foo.py"]}]
    }
    assert validate(taxonomy) == []


def test_bad_id_mentions_kebab_pattern() -> None:
    taxonomy = {
        "modules": [{"id": "BadId", "description": "bar", "paths": ["src/foo.py"]}]
    }
    errors = validate(taxonomy)
    assert errors
    assert any("[a-z]" in e for e in errors)


def test_empty_paths_mentions_minitems() -> None:
    taxonomy = {"modules": [{"id": "foo", "description": "bar", "paths": []}]}
    errors = validate(taxonomy)
    assert errors
    assert any(
        "non-empty" in e.lower() or "minitems" in e.lower() or "short" in e.lower()
        for e in errors
    )
