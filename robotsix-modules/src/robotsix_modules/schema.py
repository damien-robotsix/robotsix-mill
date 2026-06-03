"""Access to the bundled module-taxonomy JSON Schema."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml

#: Filesystem path to the bundled JSON Schema resource.
SCHEMA_PATH: Path = Path(
    str(files("robotsix_modules.schemas").joinpath("modules.schema.yaml"))
)


def load_schema() -> dict:
    """Parse and return the bundled JSON Schema as a dict."""
    text = (
        files("robotsix_modules.schemas")
        .joinpath("modules.schema.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)
