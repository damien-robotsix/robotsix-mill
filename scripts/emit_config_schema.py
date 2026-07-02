"""Generate / check the deploy-facing JSON Schema for mill config.

Usage (from the repo root):
    python scripts/emit_config_schema.py              # generate & write
    python scripts/emit_config_schema.py --check      # CI diff check

Exit codes:
    0 — success (or check passes)
    1 — check mode found drift
"""

import copy
import json
import sys
from pathlib import Path

SCHEMA_PATH = Path("config/config.schema.json")


def build_schema() -> dict:
    from robotsix_mill.config.secrets import Secrets
    from robotsix_mill.config.settings import Settings

    settings_schema = Settings.model_json_schema()

    secrets_schema = Secrets.model_json_schema()
    secrets_schema = copy.deepcopy(secrets_schema)
    # Pydantic renders ``str | None`` as
    #   {"anyOf": [{"type": "string"}, {"type": "null"}]}
    # Add markers at the property level (not inside anyOf) so the deploy
    # UI sees them.
    for prop in secrets_schema.get("properties", {}).values():
        prop["format"] = "password"
        prop["writeOnly"] = True

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Mill Configuration",
        "type": "object",
        "properties": {
            "settings": settings_schema,
            "secrets": secrets_schema,
        },
    }


def main() -> None:
    check_mode = "--check" in sys.argv
    generated = json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"

    if check_mode:
        if not SCHEMA_PATH.exists():
            print(
                f"ERROR: {SCHEMA_PATH} is missing. "
                "Run: uv run python scripts/emit_config_schema.py",
                file=sys.stderr,
            )
            sys.exit(1)
        if SCHEMA_PATH.read_text() != generated:
            print(
                f"ERROR: {SCHEMA_PATH} is stale. "
                "Run: uv run python scripts/emit_config_schema.py",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"{SCHEMA_PATH} is in sync.")
    else:
        SCHEMA_PATH.write_text(generated)
        print(f"Written {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
