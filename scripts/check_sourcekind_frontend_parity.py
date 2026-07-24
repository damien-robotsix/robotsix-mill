#!/usr/bin/env python3
"""Validate that every Python ``SourceKind`` value is reflected in the JS
``SOURCE_CLASS`` map and the CSS ``.src-*`` rules.

For every ``SourceKind`` member whose ``.value`` is not ``"user"``
(``user`` is the intentional JS fallback in ``srcClass()``):

- The JS ``SOURCE_CLASS`` object must contain a key matching the
  SourceKind value string.
- ``board-mill.css`` must contain a ``.src-{css_class}`` rule where
  ``css_class`` is the JS SOURCE_CLASS mapping value (after converting
  underscores to hyphens).
- The JS object must not contain duplicate keys.

Exits 0 when parity is clean, 1 when drift is detected.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_sourcekind_values() -> set[str]:
    """Parse SourceKind enum values from models.py."""
    models_path = REPO_ROOT / "src" / "robotsix_mill" / "core" / "models.py"
    source = models_path.read_text()

    values: set[str] = set()
    in_sourcekind = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("class SourceKind"):
            in_sourcekind = True
            continue
        if in_sourcekind:
            if stripped.startswith("class ") or stripped.startswith("def "):
                break
            m = re.match(r'^(\w+)\s*=\s*"([^"]+)"', stripped)
            if m:
                values.add(m.group(2))
    return values


def parse_js_source_class() -> dict[str, str]:
    """Parse SOURCE_CLASS object from board-mill.js.

    Returns {SourceKind_value: css_class_suffix}.
    Exits with error on duplicate keys.
    """
    js_path = (
        REPO_ROOT / "src" / "robotsix_mill" / "runtime" / "static" / "board-mill.js"
    )
    source = js_path.read_text()

    m = re.search(r"const SOURCE_CLASS\s*=\s*\{(.*?)\};", source, re.DOTALL)
    if not m:
        print("ERROR: Could not find SOURCE_CLASS object in board-mill.js")
        sys.exit(1)

    body = m.group(1)
    mapping: dict[str, str] = {}
    duplicates: list[str] = []

    for line in body.splitlines():
        entry = re.match(
            r'^\s*("(?P<qkey>[^"]+)"|(?P<ukey>\w+))\s*:\s*"(?P<val>[^"]+)"',
            line.strip(),
        )
        if entry:
            key = entry.group("qkey") or entry.group("ukey")
            val = entry.group("val")
            if key in mapping:
                duplicates.append(key)
            mapping[key] = val

    if duplicates:
        print(f"ERROR: Duplicate keys in JS SOURCE_CLASS: {', '.join(duplicates)}")
        sys.exit(1)

    return mapping


def parse_css_src_classes() -> set[str]:
    """Parse .src-* CSS class suffixes from board-mill.css."""
    css_path = (
        REPO_ROOT / "src" / "robotsix_mill" / "runtime" / "static" / "board-mill.css"
    )
    source = css_path.read_text()

    classes: set[str] = set()
    for m in re.finditer(r"\.src-([a-zA-Z0-9_-]+)\b", source):
        classes.add(m.group(1))
    return classes


def main() -> int:
    sourcekind_values = load_sourcekind_values()
    js_mapping = parse_js_source_class()
    css_classes = parse_css_src_classes()

    errors: list[str] = []

    # user is the intentional fallback — not required in JS/CSS
    values_to_check = sourcekind_values - {"user"}

    for value in sorted(values_to_check):
        if value not in js_mapping:
            errors.append(f"JS SOURCE_CLASS missing key: {value!r}")
            continue

        css_class = js_mapping[value]
        if css_class not in css_classes:
            errors.append(f"CSS missing .src-{css_class} rule (SourceKind {value!r})")

    for js_key in sorted(js_mapping):
        if js_key not in sourcekind_values:
            errors.append(
                f"JS SOURCE_CLASS has key {js_key!r} with no matching SourceKind"
            )

    if errors:
        print("SourceKind ↔ frontend parity drift detected:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(
        f"OK: {len(values_to_check)} SourceKind values all present in "
        f"JS SOURCE_CLASS and CSS."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
