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
import importlib
import sys

import httpx

from ..config import Settings
from ..core.states import State


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(base_url=settings.api_url, timeout=30.0)


from ._runners import _RUNNERS  # noqa: E402
from ._output import print_result  # noqa: E402


def _run_and_print(cmd: str, args: argparse.Namespace) -> int:
    """Dynamically import and run a subcommand's runner, then print results."""
    entry = _RUNNERS[cmd]
    mod = importlib.import_module(f".{entry['module']}", package="robotsix_mill")
    func = getattr(mod, entry["function"])

    try:
        if cmd == "trace-health":
            result = func()
        elif cmd == "verify":
            from ..runtime.tracing import make_session_id

            session_id = make_session_id(cmd)
            ticket_id = getattr(args, "ticket_id", None)
            result = func(session_id=session_id, ticket_id=ticket_id)
        elif cmd == "langfuse-cleanup":
            from ..config import Settings

            settings = Settings()
            result = func(
                settings=settings,
                repo_config=None,
                max_traces=settings.langfuse_cleanup_max_traces,
            )
        elif cmd == "member-sync":
            from ..runtime.tracing import make_session_id
            from ..config import get_repos_config

            session_id = make_session_id(cmd)
            repo_id = _resolve_repo_id(args)
            if repo_id is None:
                return 2
            repos = get_repos_config()
            if repo_id not in repos.repos:
                print(
                    f"member-sync: unknown repo '{repo_id}'. "
                    f"Known repos: {sorted(repos.repos.keys())}",
                    file=sys.stderr,
                )
                return 2
            rc = repos.repos[repo_id]
            result = func(session_id=session_id, repo_config=rc)
        elif cmd == "trace-review":
            from ..runtime.tracing import make_session_id
            from ..config import get_repos_config

            session_id = make_session_id(cmd)
            repo_id = _resolve_repo_id(args)
            if repo_id is None:
                return 2
            repos = get_repos_config()
            if repo_id not in repos.repos:
                print(
                    f"trace-review: unknown repo '{repo_id}'. "
                    f"Known repos: {sorted(repos.repos.keys())}",
                    file=sys.stderr,
                )
                return 2
            rc = repos.repos[repo_id]
            result = func(session_id=session_id, repo_config=rc)
        elif cmd == "roadmap-sync":
            from ..runtime.tracing import make_session_id
            from ..config import get_repos_config

            session_id = make_session_id(cmd)
            repo_id = _resolve_repo_id(args)
            if repo_id is None:
                return 2
            repos = get_repos_config()
            if repo_id not in repos.repos:
                print(
                    f"roadmap-sync: unknown repo '{repo_id}'. "
                    f"Known repos: {sorted(repos.repos.keys())}",
                    file=sys.stderr,
                )
                return 2
            rc = repos.repos[repo_id]
            result = func(session_id=session_id, repo_config=rc)
        elif cmd == "meta":
            from ..runtime.tracing import make_session_id

            session_id = make_session_id(cmd)
            result = func(session_id=session_id)
            # Combine the three draft lists for the memory_drafts format handler.
            result.drafts_created = (
                result.extraction_drafts_created
                + result.alignment_drafts_created
                + result.todo_drafts_created
            )
        else:
            from ..runtime.tracing import make_session_id

            session_id = make_session_id(cmd)
            result = func(session_id=session_id)
    except Exception as e:
        print(f"{cmd} failed: {e}", file=sys.stderr)
        return 1

    print_result(entry, result, args)
    return 0


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


def build_parser() -> argparse.ArgumentParser:
    """Build and return the robotsix-mill argument parser.

    Separated from ``main()`` so that completion-script generators
    (e.g. ``scripts/gen_completions.py``) can introspect the parser
    without side effects.
    """
    parser = argparse.ArgumentParser(prog="robotsix-mill")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="run the API + event-driven worker")
    p_serve.add_argument(
        "--repo-id",
        default=None,
        help="repository identifier to serve a single repo; "
        "omit to serve all repos from config/repos.yaml",
    )

    # --- repos list command ---
    p_repos = sub.add_parser("repos", help="repository operations")
    rsub = p_repos.add_subparsers(dest="rcmd", required=True)
    rsub.add_parser("list", help="list registered repos and their boards")

    p_ticket = sub.add_parser("ticket", help="ticket operations")
    tsub = p_ticket.add_subparsers(dest="tcmd", required=True)

    p_new = tsub.add_parser("new", help="emit a ticket (worker picks it up)")
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--description-file", help="file with the body; '-' reads stdin")
    p_new.add_argument(
        "--repo-id",
        help="repository identifier (required when multiple repos are "
        "registered; optional when only one)",
    )
    p_new.add_argument(
        "--screenshot",
        action="append",
        help="attach an image file to the ticket (repeatable)",
    )

    p_list = tsub.add_parser("list", help="list tickets")
    p_list.add_argument("--state", choices=[s.value for s in State])
    p_list.add_argument(
        "--repo-id",
        help="filter tickets by repository (list all when omitted)",
    )

    p_show = tsub.add_parser("show", help="show one ticket + history")
    p_show.add_argument("id")

    p_approve = tsub.add_parser(
        "approve", help="approve a ticket in human_issue_approval state"
    )
    p_approve.add_argument("id")

    p_resume = tsub.add_parser(
        "resume-blocked",
        help="resume a blocked ticket back to the state it was blocked from",
    )
    p_resume.add_argument("id")

    # --- audit command ---
    p_audit = sub.add_parser("audit", help="run an audit pass and emit gap drafts")
    p_audit.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- trace-health command ---
    p_trace_health = sub.add_parser(
        "trace-health",
        help="check Langfuse for unsessioned / unnamed traces and alert if found",
    )
    p_trace_health.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- langfuse-cleanup command ---
    p_langfuse_cleanup = sub.add_parser(
        "langfuse-cleanup",
        help="delete excess Langfuse traces to stay under the per-project cap",
    )
    p_langfuse_cleanup.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- health command ---
    p_health = sub.add_parser("health", help="run a health pass and emit gap drafts")
    p_health.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- agent-check command ---
    p_agent_check = sub.add_parser(
        "agent-check", help="run an agent definition coherence check"
    )
    p_agent_check.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- verify command ---
    p_verify = sub.add_parser("verify", help="verify TicketEvent hash-chain integrity")
    p_verify.add_argument(
        "--ticket-id",
        default=None,
        help="verify a single ticket's chain (default: all tickets)",
    )

    # --- test-gap command ---
    p_test_gap = sub.add_parser(
        "test-gap", help="run a test-gap coverage inspection pass"
    )
    p_test_gap.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- config-sync command ---
    p_config_sync = sub.add_parser(
        "config-sync", help="run a config-sync config/docs drift detection pass"
    )
    p_config_sync.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- member-sync command ---
    p_member_sync = sub.add_parser(
        "member-sync",
        help="run a workspace member-sync (vcs2l manifest) pass",
    )
    p_member_sync.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )
    p_member_sync.add_argument(
        "--repo-id",
        help="master repo to sync members from (required if multiple repos)",
    )

    # --- bc-check command ---
    p_bc_check = sub.add_parser(
        "bc-check", help="run a backward-compatibility inspection pass"
    )
    p_bc_check.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- completeness-check command ---
    p_completeness_check = sub.add_parser(
        "completeness-check", help="run a feature-completeness inspection pass"
    )
    p_completeness_check.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- survey command ---
    p_survey = sub.add_parser("survey", help="run an OSS project discovery survey pass")
    p_survey.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- copy-paste command ---
    p_copy_paste = sub.add_parser(
        "copy-paste", help="run a copy-paste / code-duplication detection pass"
    )
    p_copy_paste.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- forge-parity command ---
    p_forge_parity = sub.add_parser(
        "forge-parity", help="run a forge-parity drift detection pass"
    )
    p_forge_parity.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- run-health command ---
    p_run_health = sub.add_parser(
        "run-health",
        help="run a health analysis pass over recent run outcomes",
    )
    p_run_health.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- diagnostic command ---
    p_diagnostic = sub.add_parser(
        "diagnostic",
        help="run a single daily-diagnostic pass over the registered checks",
    )
    p_diagnostic.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- trace-review command ---
    p_trace_review = sub.add_parser(
        "trace-review",
        help="run a trace-review pass over recent Langfuse traces",
    )
    p_trace_review.add_argument(
        "--json",
        action="store_true",
        help="output raw JSON instead of human-friendly text",
    )
    p_trace_review.add_argument(
        "--repo-id",
        help="repository to run trace-review for (required if multiple repos)",
    )

    # --- roadmap-sync command ---
    p_roadmap_sync = sub.add_parser(
        "roadmap-sync",
        help="run a roadmap-sync pass",
    )
    p_roadmap_sync.add_argument(
        "--json",
        action="store_true",
        help="output raw JSON instead of human-friendly text",
    )
    p_roadmap_sync.add_argument(
        "--repo-id",
        help="repository to run roadmap-sync for (required if multiple repos)",
    )

    # --- module-curator command ---
    p_module_curator = sub.add_parser(
        "module-curator", help="run a module taxonomy drift detection pass"
    )
    p_module_curator.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- meta command ---
    _p_meta = sub.add_parser(
        "meta",
        help="Run the meta pass (extraction + alignment across all repos)",
    )
    _p_meta.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- inquire command ---
    p_inquire = sub.add_parser(
        "inquire", help="ask a one-shot question (no code-change lifecycle)"
    )
    p_inquire.add_argument("--title", required=True)
    p_inquire.add_argument(
        "--description-file", help="file with the question body; '-' reads stdin"
    )

    # --- epic command ---
    p_epic = sub.add_parser("epic", help="epic operations")
    esub = p_epic.add_subparsers(dest="ecmd", required=True)

    p_epic_new = esub.add_parser("new", help="create a new epic")
    p_epic_new.add_argument("--title", required=True)
    p_epic_new.add_argument(
        "--description-file", help="file with the description; '-' reads stdin"
    )
    p_epic_new.add_argument(
        "--repo-id",
        help="repository identifier (required when multiple repos are "
        "registered; optional when only one)",
    )

    # --print-completion (shtab shell completion)
    parser.add_argument(
        "--print-completion",
        choices=["bash", "zsh", "tcsh"],
        help="print shell completion script for the specified shell",
    )

    return parser


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
