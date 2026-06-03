"""Command-line entry points for robotsix-modules."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from . import __version__, load_taxonomy, validate

PROG = "robotsix-modules"


def _validate_one(path: str, schema_path: str | None) -> int:
    """Validate a single taxonomy file. Return the process exit code."""
    try:
        taxonomy = load_taxonomy(path)
    except FileNotFoundError:
        print(f"{PROG}: error: file not found: {path}", file=sys.stderr)
        return 2
    except yaml.YAMLError as exc:
        print(f"{PROG}: error: invalid YAML in {path}: {exc}", file=sys.stderr)
        return 2

    schema: dict | None = None
    if schema_path is not None:
        try:
            schema = yaml.safe_load(Path(schema_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(
                f"{PROG}: error: schema file not found: {schema_path}",
                file=sys.stderr,
            )
            return 2
        except yaml.YAMLError as exc:
            print(
                f"{PROG}: error: invalid YAML in schema {schema_path}: {exc}",
                file=sys.stderr,
            )
            return 2

    errors = validate(taxonomy, schema=schema)
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate a module-taxonomy file."
    )
    validate_parser.add_argument("path", help="Path to the taxonomy YAML file.")
    validate_parser.add_argument(
        "--schema",
        dest="schema",
        default=None,
        help="Override the bundled JSON Schema with the given file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Top-level CLI: ``robotsix-modules``."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _validate_one(args.path, args.schema)
    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits before reaching here


def validate_main(argv: list[str] | None = None) -> int:
    """Lightweight pre-commit wrapper: ``robotsix-modules-validate``.

    Accepts one or more positional paths (pre-commit passes each matched
    file as a separate argument). Same exit-code semantics as
    ``robotsix-modules validate``.
    """
    parser = argparse.ArgumentParser(prog=f"{PROG}-validate")
    parser.add_argument(
        "paths",
        nargs="+",
        help="One or more taxonomy YAML files to validate.",
    )
    parser.add_argument(
        "--schema",
        dest="schema",
        default=None,
        help="Override the bundled JSON Schema with the given file.",
    )
    args = parser.parse_args(argv)

    exit_code = 0
    for path in args.paths:
        code = _validate_one(path, args.schema)
        exit_code = max(exit_code, code)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
