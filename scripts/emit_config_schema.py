"""Generate / check the deploy-facing JSON Schema for mill config.

Usage (from the repo root):
    python scripts/emit_config_schema.py              # generate & write
    python scripts/emit_config_schema.py --check      # CI diff check

Exit codes:
    0 — success (or check passes)
    1 — check mode found drift
"""

import copy
import difflib
import json
import sys
from pathlib import Path

SCHEMA_PATH = Path("config/config.schema.json")

# Max lines of unified diff to emit when --check finds drift.
_MAX_DIFF_LINES = 200


def _hoist_defs(schema: dict, root_defs: dict) -> dict:
    """Move ``$defs`` from *schema* into *root_defs* and strip them locally.

    Pydantic's ``model_json_schema()`` places ``$defs`` at the top of
    each model's generated schema with ``$ref`` paths like
    ``#/$defs/Foo``.  When we nest those raw schemas under
    ``properties.repos``, the ``$defs`` end up at
    ``properties.repos.$defs`` while the ``$ref`` values still point to
    the document root — unresolvable for a spec-compliant validator.

    This helper extracts every schema's local ``$defs`` into the shared
    root-level ``$defs`` dict so ``$ref`` paths resolve correctly.
    """
    local_defs = schema.pop("$defs", None)
    if isinstance(local_defs, dict):
        for def_name, def_schema in local_defs.items():
            if def_name not in root_defs:
                root_defs[def_name] = def_schema
    return schema


def _compute_diff(
    committed: str,
    generated: str,
    fromfile: str = "committed",
    tofile: str = "generated",
    max_lines: int = _MAX_DIFF_LINES,
) -> str:
    """Return a unified diff between *committed* and *generated*.

    Returns an empty string when the two texts are identical.
    Caps output at *max_lines* lines to avoid flooding logs.
    """
    if committed == generated:
        return ""
    committed_lines = committed.splitlines(keepends=True)
    generated_lines = generated.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            committed_lines,
            generated_lines,
            fromfile=fromfile,
            tofile=tofile,
        )
    )
    if len(diff) > max_lines:
        truncated = diff[:max_lines]
        truncated.append(f"... (truncated; {len(diff) - max_lines} more lines)\n")
        return "".join(truncated)
    return "".join(diff)


def build_schema() -> dict:
    from robotsix_mill.config.repos import ReposRegistry
    from robotsix_mill.config.secrets import Secrets
    from robotsix_mill.config.settings import Settings

    root_defs: dict = {}

    settings_schema = Settings.model_json_schema()
    _hoist_defs(settings_schema, root_defs)

    repos_schema = ReposRegistry.model_json_schema()
    _hoist_defs(repos_schema, root_defs)

    secrets_schema = Secrets.model_json_schema()
    _hoist_defs(secrets_schema, root_defs)
    secrets_schema = copy.deepcopy(secrets_schema)
    # Pydantic renders ``str | None`` as
    #   {"anyOf": [{"type": "string"}, {"type": "null"}]}
    # Add markers at the property level (not inside anyOf) so the deploy
    # UI sees them.
    for prop in secrets_schema.get("properties", {}).values():
        prop["format"] = "password"
        prop["writeOnly"] = True

    result: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Mill Configuration",
        "type": "object",
        "properties": {
            "settings": settings_schema,
            "secrets": secrets_schema,
            "repos": repos_schema,
        },
    }
    if root_defs:
        result["$defs"] = root_defs
    return result


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
        committed = SCHEMA_PATH.read_text()
        if committed != generated:
            diff_text = _compute_diff(
                committed,
                generated,
                fromfile=f"{SCHEMA_PATH} (committed)",
                tofile="generated",
            )
            if diff_text:
                print(diff_text, file=sys.stderr, end="")
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
