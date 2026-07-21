#!/usr/bin/env python3
"""Regenerate config/config.schema.json from pydantic models."""

import sys

sys.path.insert(0, "/repo")

import copy
import json
from pathlib import Path

from robotsix_mill.config.repos import ReposRegistry
from robotsix_mill.config.secrets import Secrets
from robotsix_mill.config.settings import Settings

SCHEMA_PATH = Path("/repo/config/config.schema.json")

root_defs = {}


def hoist_defs(schema, root_defs):
    local_defs = schema.pop("$defs", None)
    if isinstance(local_defs, dict):
        for def_name, def_schema in local_defs.items():
            if def_name not in root_defs:
                root_defs[def_name] = def_schema
    return schema


settings_schema = Settings.model_json_schema()
hoist_defs(settings_schema, root_defs)

repos_schema = ReposRegistry.model_json_schema()
hoist_defs(repos_schema, root_defs)

secrets_schema = Secrets.model_json_schema()
hoist_defs(secrets_schema, root_defs)
secrets_schema = copy.deepcopy(secrets_schema)
for prop in secrets_schema.get("properties", {}).values():
    prop["format"] = "password"
    prop["writeOnly"] = True

result = {
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

output = json.dumps(result, indent=2, sort_keys=True) + "\n"
SCHEMA_PATH.write_text(output)
print("Written", SCHEMA_PATH)
