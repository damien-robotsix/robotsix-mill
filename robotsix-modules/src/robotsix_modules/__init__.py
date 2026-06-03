"""Shared JSON-Schema-driven module-taxonomy validation for robotsix projects.

Public API:
    load_taxonomy(path)             -> dict
    validate(taxonomy, *, schema)   -> list[str]
    validate_file(path, *, schema_path) -> list[str]
    SCHEMA_PATH                     -> pathlib.Path
    __version__                     -> str
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .schema import SCHEMA_PATH, load_schema

__version__ = "0.1.0"

__all__ = [
    "SCHEMA_PATH",
    "__version__",
    "load_taxonomy",
    "validate",
    "validate_file",
]


def load_taxonomy(path: str | Path) -> dict:
    """Load a ``modules.yaml`` file and return it as a dict.

    Raises:
        FileNotFoundError: when ``path`` does not exist.
        yaml.YAMLError: when the file is not valid YAML.
    """
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _format_error(error) -> str:
    """Render a jsonschema ValidationError as a debuggable one-liner.

    Includes the JSON pointer to the offending node and the violating
    value, e.g. ``modules[2].id: 'BadId' does not match '^[a-z]...'``.
    """
    parts: list[str] = []
    for token in error.absolute_path:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        elif parts:
            parts.append(f".{token}")
        else:
            parts.append(str(token))
    pointer = "".join(parts) or "<root>"
    return f"{pointer}: {error.message}"


def validate(taxonomy: dict, *, schema: dict | None = None) -> list[str]:
    """Validate a taxonomy dict against a JSON Schema.

    Args:
        taxonomy: parsed YAML/JSON object to validate.
        schema: optional override for the bundled schema.

    Returns:
        List of human-readable error messages. Empty list = valid.
    """
    active_schema = schema if schema is not None else load_schema()
    validator = Draft202012Validator(active_schema)
    errors = sorted(validator.iter_errors(taxonomy), key=lambda e: list(e.absolute_path))
    return [_format_error(error) for error in errors]


def validate_file(
    path: str | Path, *, schema_path: str | Path | None = None
) -> list[str]:
    """Convenience: ``load_taxonomy`` then ``validate``.

    Same return contract as :func:`validate`.
    """
    taxonomy = load_taxonomy(path)
    schema: dict | None = None
    if schema_path is not None:
        schema = yaml.safe_load(Path(schema_path).read_text(encoding="utf-8"))
    return validate(taxonomy, schema=schema)
