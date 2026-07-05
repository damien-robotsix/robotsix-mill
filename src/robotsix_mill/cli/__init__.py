"""``robotsix-mill`` CLI — a thin HTTP client over the service.

    robotsix-mill serve                       # run the API + worker
    robotsix-mill ticket new --title T [--description-file F | -]
    robotsix-mill ticket list [--state S]
    robotsix-mill ticket show <id>
    robotsix-mill ticket approve <id>
    robotsix-mill ticket resume-blocked <id>
    robotsix-mill epic new --title T [--description-file F | -]
    robotsix-mill inquire --title T [--description-file F | -]
    robotsix-mill audit                        # run an audit pass
    robotsix-mill trace-health                 # run a trace-health check
    robotsix-mill health                        # run a health pass
    robotsix-mill copy-paste                   # run a copy-paste detection pass

The same API backs a future web frontend.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from ..config import Settings


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(base_url=settings.api_url, timeout=30.0)


def _read_body_from_args(args: argparse.Namespace) -> str:
    """Read a description body from --description-file (file path or '-' for stdin)."""
    if args.description_file == "-":
        return sys.stdin.read()
    if args.description_file:
        with open(args.description_file, encoding="utf-8") as f:
            return f.read()
    return ""


def _resolve_repo_id(
    args: argparse.Namespace, returncode_on_failure: int = 2
) -> str | None:
    """Resolve repo_id from args, handling multi-repo config.

    Returns repo_id on success or None on failure (caller should
    ``return returncode_on_failure``).
    """
    if args.repo_id is not None:
        repo_id: str = args.repo_id
        return repo_id

    from ..config import get_repos_config
    from ..config import ConfigError as _ConfigError

    try:
        repos = get_repos_config()
    except _ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None

    if len(repos.repos) == 1:
        return next(iter(repos.repos.keys()))
    if not repos.repos:
        print("Error: no repos defined in config/repos.yaml", file=sys.stderr)
        return None

    sorted_keys = sorted(repos.repos.keys())
    print(
        f"Error: --repo-id is required when multiple repos are configured. "
        f"Available repos: {sorted_keys}",
        file=sys.stderr,
    )
    return None


# Subcommand implementations live in sibling modules of this package.
# These imports must appear after the helpers above to avoid circular imports.
from .serve import _serve, _repos_list  # noqa: E402
from .ticket import (  # noqa: E402
    _ticket_new,
    _ticket_list,
    _ticket_show,
    _ticket_approve,
    _ticket_resume_blocked,
)
from .epic import _epic_new  # noqa: E402
from .inquire import _inquire  # noqa: E402


# build_parser is the parser factory, separated so that completion-script
# generators (e.g. scripts/gen_completions.py) can introspect it without
# side effects.
#
# _RUNNERS and _run_and_print are the generic admin-command dispatch —
# dynamic import of runner modules with JSON/human-readable output.
from ._parser import build_parser  # noqa: E402
from .admin import _RUNNERS, _run_and_print  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Entry point for the robotsix-mill CLI.

    Available subcommands:

    * ``serve`` — run the API and event-driven worker
    * ``ticket new|list|show|approve|resume-blocked`` — ticket lifecycle
      operations
    * ``audit`` — run an audit pass and emit gap drafts
    * ``trace-health`` — check Langfuse for unsessioned / unnamed traces
    * ``health`` — run a health pass and emit gap drafts
    * ``copy-paste`` — run a copy-paste / code-duplication detection pass

    Returns 0 on success, nonzero on failure.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "print_completion", None):
        try:
            import shtab
        except ImportError:
            print(
                "shtab is required for --print-completion. "
                "Install it with: pip install shtab",
                file=sys.stderr,
            )
            return 1
        print(shtab.complete(parser, shell=args.print_completion))  # nosec B604 -- shell= is a shell name, not shell=True
        return 0

    settings = Settings()

    if args.cmd == "serve":
        return _serve(args, settings)
    if args.cmd == "repos" and args.rcmd == "list":
        return _repos_list(args, settings)
    if args.cmd in _RUNNERS:
        return _run_and_print(args.cmd, args)
    if args.cmd == "inquire":
        return _inquire(args, settings)
    if args.cmd == "epic" and args.ecmd == "new":
        return _epic_new(args, settings)
    if args.cmd == "ticket":
        if args.tcmd == "new":
            return _ticket_new(args, settings)
        if args.tcmd == "list":
            return _ticket_list(args, settings)
        if args.tcmd == "show":
            return _ticket_show(args, settings)
        if args.tcmd == "approve":
            return _ticket_approve(args, settings)
        if args.tcmd == "resume-blocked":
            return _ticket_resume_blocked(args, settings)

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
