"""Flatten a nested config dict into ``{alias: value}`` via a path map."""

from __future__ import annotations

from typing import Any


def flatten_config(nested: dict, alias_map: dict[str, str]) -> dict[str, Any]:
    """Flatten *nested* into a flat ``{alias: value}`` dict.

    Walks *nested* building dotted-path keys (``"a.b.c"``).  For each
    path, look it up in *alias_map*; if found, emit ``result[alias] =
    value`` AS-IS — including dict-valued aliases — and stop descending
    that branch.  If the path is not mapped and the value is a dict,
    recurse.  Unknown leaf paths are silently dropped.  When the same
    alias is reachable via multiple paths, the last-traversed wins
    (dict insertion order).
    """
    result: dict[str, Any] = {}

    def _walk(d: dict, prefix: str = "") -> None:
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            alias = alias_map.get(full_key)
            if alias is not None:
                result[alias] = value
                continue
            if isinstance(value, dict):
                _walk(value, full_key)

    _walk(nested)
    return result
