"""Generate / check the deploy-facing JSON Schema for mill config.

Usage (from the repo root):
    python scripts/emit_config_schema.py              # generate & write
    python scripts/emit_config_schema.py --check      # CI diff check

Exit codes:
    0 — success (or check passes)
    1 — check mode found drift
"""

import json
import sys
from pathlib import Path

from robotsix_config import config_schema

SCHEMA_PATH = Path("config/config.schema.json")


def build_schema() -> dict:
    from robotsix_mill.config.mill_config import MillConfig

    return config_schema(MillConfig)


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
