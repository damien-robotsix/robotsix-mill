# vulture_whitelist.py — framework-invoked names that vulture would otherwise
# flag as unused. Keep this file: vulture scans it alongside the source tree
# and considers names referenced here as "used".
#
# This file is NOT imported — bare-name expressions at module scope are
# evaluated by Python (no-op) and seen as usage by vulture's AST scan.
# If a name no longer exists in the source tree, Python raises a NameError
# at scan time, catching stale entries.

# ---------------------------------------------------------------------------
# CLI entry points (called via console_scripts in pyproject.toml)
# ---------------------------------------------------------------------------
# cli/__init__.py: main() — wired as robotsix-mill
# autoupdate/__init__.py: main() — wired as robotsix-autoupdate
main  # noqa: F821 — robotsix_mill.cli:main, robotsix_mill.autoupdate:main

# ---------------------------------------------------------------------------
# Pydantic validator methods — decorated with @field_validator / @model_validator
# and invoked by pydantic's metaclass machinery (vulture may flag the method name
# if the class is used but the method body introspects are hidden from AST).
# ---------------------------------------------------------------------------
# If vulture flags any, add the bare method name here.  Currently this section
# is empty because vulture v2+ recognises decorated methods as used.
