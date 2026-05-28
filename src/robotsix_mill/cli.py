"""``robotsix-mill`` CLI — a thin HTTP client over the service.

    robotsix-mill serve                       # run the API + worker
    robotsix-mill ticket new --title T [--description-file F | -]
    robotsix-mill ticket list [--state S]
    robotsix-mill ticket show <id>
    robotsix-mill ticket approve <id>
    robotsix-mill ticket resume-blocked <id>
    robotsix-mill inquire --title T [--description-file F | -]
    robotsix-mill audit                        # run an audit pass
    robotsix-mill trace-health                 # run a trace-health check
    robotsix-mill health                        # run a health pass

The same API backs a future web frontend.
"""

from __future__ import annotations

import argparse
import json
import importlib
import sys

import httpx

from .config import Settings
from .core.states import State


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(base_url=settings.api_url, timeout=30.0)


_RUNNERS: dict[str, dict[str, str]] = {
    "audit": {
        "module": "audit_runner",
        "function": "run_audit_pass",
        "label": "Audit pass",
        "format": "memory_drafts",
    },
    "health": {
        "module": "health_runner",
        "function": "run_health_pass",
        "label": "Health pass",
        "format": "memory_drafts",
    },
    "agent-check": {
        "module": "agent_check_runner",
        "function": "run_agent_check_pass",
        "label": "Agent-check pass",
        "format": "memory_drafts",
    },
    "test-gap": {
        "module": "test_gap_runner",
        "function": "run_test_gap_pass",
        "label": "Test-gap pass",
        "format": "memory_drafts",
    },
    "config-sync": {
        "module": "config_sync_runner",
        "function": "run_config_sync_pass",
        "label": "Config-sync pass",
        "format": "memory_drafts",
    },
    "trace-health": {
        "module": "trace_health_runner",
        "function": "run_trace_health_check",
        "label": "Trace-health check",
        "format": "trace_health",
    },
    "langfuse-cleanup": {
        "module": "langfuse_cleanup_runner",
        "function": "run_langfuse_cleanup_pass",
        "label": "Langfuse cleanup pass",
        "format": "langfuse_cleanup",
    },
    "bc-check": {
        "module": "bc_check_runner",
        "function": "run_bc_check_pass",
        "label": "BC-check pass",
        "format": "memory_drafts",
    },
    "completeness-check": {
        "module": "completeness_check_runner",
        "function": "run_completeness_check_pass",
        "label": "Completeness-check pass",
        "format": "memory_drafts",
    },
    "cost-reconciliation": {
        "module": "cost_reconciliation_runner",
        "function": "run_cost_reconciliation_pass",
        "label": "Cost-reconciliation pass",
        "format": "memory_drafts",
    },
}


def _run_and_print(cmd: str, args: argparse.Namespace) -> int:
    """Dynamically import and run a subcommand's runner, then print results."""
    entry = _RUNNERS[cmd]
    mod = importlib.import_module(
        f".{entry['module']}", package="robotsix_mill"
    )
    func = getattr(mod, entry["function"])

    try:
        if cmd == "trace-health":
            result = func()
        elif cmd == "langfuse-cleanup":
            from .config import Settings
            settings = Settings()
            result = func(settings=settings, repo_config=None, max_traces=settings.langfuse_cleanup_max_traces)
        elif cmd == "cost-reconciliation":
            from .runtime.tracing import make_session_id
            session_id = make_session_id(cmd)
            repo_id = getattr(args, "repo_id", None)
            if repo_id:
                from .config import get_repos_config
                repos = get_repos_config()
                if repo_id not in repos.repos:
                    sorted_keys = sorted(repos.repos.keys())
                    print(
                        f"cost-reconciliation: unknown repo '{repo_id}'. "
                        f"Known repos: {sorted_keys}",
                        file=sys.stderr,
                    )
                    return 2
                rc = repos.repos[repo_id]
                result = func(session_id=session_id, repo_config=rc)
            else:
                result = func(session_id=session_id)
        else:
            from .runtime.tracing import make_session_id

            session_id = make_session_id(cmd)
            result = func(session_id=session_id)
    except Exception as e:
        print(f"{cmd} failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        if entry["format"] == "trace_health":
            print(
                json.dumps(
                    {
                        "draft_created": result.draft_created,
                        "unsessioned_count": result.unsessioned_count,
                        "total_traces": result.total_traces,
                        "window_start": result.window_start,
                        "window_end": result.window_end,
                    },
                    indent=2,
                )
            )
        elif entry["format"] == "langfuse_cleanup":
            print(
                json.dumps(
                    {
                        "project": result.project,
                        "traces_before": result.traces_before,
                        "traces_deleted": result.traces_deleted,
                    },
                    indent=2,
                )
            )
        else:
            print(
                json.dumps(
                    {
                        "memory": result.updated_memory,
                        "tickets_created": result.drafts_created,
                    },
                    indent=2,
                )
            )
    else:
        if entry["format"] == "trace_health":
            print("Trace-health check complete.")
            if result.draft_created:
                print("Draft ticket created for unsessioned traces.")
            else:
                print("No alert needed.")
            print(
                f"Unsessoned: {result.unsessioned_count} / "
                f"{result.total_traces} traces "
                f"({result.window_start} → {result.window_end})"
            )
        elif entry["format"] == "langfuse_cleanup":
            print("Langfuse cleanup complete.")
            print(f"Project: {result.project}")
            print(f"Traces before: {result.traces_before}")
            print(f"Traces deleted: {result.traces_deleted}")
        else:
            print(f"{entry['label']} complete.")
            print(f"Memory updated: {len(result.updated_memory)} chars")
            if result.drafts_created:
                print(
                    f"Draft tickets created: {len(result.drafts_created)}"
                )
                for d in result.drafts_created:
                    print(f"  - {d['id']}: {d['title']}")
            else:
                print("No new draft tickets created.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the robotsix-mill CLI.

    Available subcommands:

    * ``serve`` — run the API and event-driven worker
    * ``ticket new|list|show|approve|resume-blocked`` — ticket lifecycle
      operations
    * ``audit`` — run an audit pass and emit gap drafts
    * ``trace-health`` — check Langfuse for unsessioned traces
    * ``health`` — run a health pass and emit gap drafts

    Returns 0 on success, nonzero on failure.
    """
    parser = argparse.ArgumentParser(prog="robotsix-mill")
    sub = parser.add_subparsers(dest="cmd", required=True)

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
    p_new.add_argument(
        "--description-file", help="file with the body; '-' reads stdin"
    )
    p_new.add_argument(
        "--repo-id",
        help="repository identifier (required when multiple repos are "
        "registered; optional when only one)",
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
    p_audit = sub.add_parser(
        "audit", help="run an audit pass and emit gap drafts"
    )
    p_audit.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- trace-health command ---
    p_trace_health = sub.add_parser(
        "trace-health",
        help="check Langfuse for unsessioned traces and alert if found",
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
    p_health = sub.add_parser(
        "health", help="run a health pass and emit gap drafts"
    )
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

    # --- cost-reconciliation command ---
    p_cost_reconciliation = sub.add_parser(
        "cost-reconciliation", help="run an OpenRouter vs Langfuse cost drift detection pass"
    )
    p_cost_reconciliation.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )
    p_cost_reconciliation.add_argument(
        "--repo-id",
        help="scope to a specific repo (default: all)",
    )

    # --- inquire command ---
    p_inquire = sub.add_parser(
        "inquire", help="ask a one-shot question (no code-change lifecycle)"
    )
    p_inquire.add_argument("--title", required=True)
    p_inquire.add_argument(
        "--description-file", help="file with the question body; '-' reads stdin"
    )

    args = parser.parse_args(argv)
    settings = Settings()

    if args.cmd == "serve":
        # Raise nofile soft cap: docker-compose's ulimits only set the
        # hard cap, and PAM (via runuser in the container entrypoint)
        # clamps the soft back to 1024. Workers cascade-crash with
        # OSError: [Errno 24] once they exhaust it across parallel
        # git/trivy/agent subprocesses.
        import resource
        try:
            _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = max(65536, hard) if hard != resource.RLIM_INFINITY else 65536
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            pass

        import uvicorn

        from .runtime.api import create_app
        from .config import get_repos_config, ReposRegistry
        from .config_loader import ConfigError

        if args.repo_id:
            # Single-repo override for tests/dev.
            try:
                repos = get_repos_config()
            except ConfigError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            if args.repo_id not in repos.repos:
                known = sorted(repos.repos.keys())
                print(
                    f"Error: Unknown repo '{args.repo_id}'. Known repos: {known}",
                    file=sys.stderr,
                )
                return 2
            single_repo_id: str | None = args.repo_id
        else:
            # Multi-repo mode: load all repos from config/repos.yaml.
            try:
                repos = get_repos_config()
            except ConfigError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            if not repos.repos:
                print(
                    "Error: no repos defined in config/repos.yaml",
                    file=sys.stderr,
                )
                return 2
            single_repo_id = None

        uvicorn.run(
            create_app(repos, settings, single_repo_id=single_repo_id),
            host=settings.api_host,
            port=settings.api_port,
        )
        return 0

    if args.cmd == "repos":
        if args.rcmd == "list":
            from .config import get_repos_config
            from .config_loader import ConfigError

            try:
                repos = get_repos_config()
            except ConfigError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            print(f"{'REPO_ID':30s} {'BOARD_ID'}")
            for rc in repos.repos.values():
                print(f"{rc.repo_id:30s} {rc.board_id}")
            return 0

    if args.cmd in _RUNNERS:
        return _run_and_print(args.cmd, args)

    if args.cmd == "inquire":
        body = ""
        if args.description_file == "-":
            body = sys.stdin.read()
        elif args.description_file:
            with open(args.description_file, encoding="utf-8") as f:
                body = f.read()
        r = c.post(
            "/tickets",
            json={"title": args.title, "description": body, "kind": "inquiry"},
        )
        r.raise_for_status()
        print(r.json()["id"])
        return 0

    with _client(settings) as c:
        if args.tcmd == "new":
            body = ""
            if args.description_file == "-":
                body = sys.stdin.read()
            elif args.description_file:
                with open(args.description_file, encoding="utf-8") as f:
                    body = f.read()
            # Resolve repo_id: required in multi-repo mode, optional in single-repo.
            repo_id = args.repo_id
            if repo_id is None:
                from .config import get_repos_config
                from .config_loader import ConfigError as _ConfigError
                try:
                    repos = get_repos_config()
                except _ConfigError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 2
                if len(repos.repos) == 1:
                    repo_id = next(iter(repos.repos.keys()))
                elif not repos.repos:
                    print("Error: no repos defined in config/repos.yaml", file=sys.stderr)
                    return 2
                else:
                    sorted_keys = sorted(repos.repos.keys())
                    print(
                        f"Error: --repo-id is required when multiple repos are configured. "
                        f"Available repos: {sorted_keys}",
                        file=sys.stderr,
                    )
                    return 2
            r = c.post(
                "/tickets",
                json={"title": args.title, "description": body, "repo_id": repo_id},
            )
            r.raise_for_status()
            print(r.json()["id"])
            return 0
        if args.tcmd == "list":
            params: dict = {"state": args.state} if args.state else {}
            if args.repo_id:
                params["repo_id"] = args.repo_id
            r = c.get("/tickets", params=params)
            r.raise_for_status()
            for t in r.json():
                print(f"{t['id']}\t{t['state']}\t{t['title']}")
            return 0
        if args.tcmd == "show":
            r = c.get(f"/tickets/{args.id}")
            r.raise_for_status()
            print(r.json())
            h = c.get(f"/tickets/{args.id}/history")
            print("--- history ---")
            for e in h.json():
                print(f"{e['at']}\t{e['state']}\t{e.get('note')}")
            return 0
        if args.tcmd == "approve":
            r = c.post(f"/tickets/{args.id}/approve")
            if r.is_success:
                data = r.json()
                print(f"ticket {data['id']} approved — now in {data['state']}")
                return 0
            else:
                try:
                    detail = r.json().get("detail", r.text)
                except Exception:
                    detail = r.text
                print(f"approve failed: {detail}", file=sys.stderr)
                return 1
        if args.tcmd == "resume-blocked":
            r = c.post(f"/tickets/{args.id}/resume-blocked")
            if r.is_success:
                data = r.json()
                print(f"ticket {data['id']} resumed — now in {data['state']}")
                return 0
            else:
                try:
                    detail = r.json().get("detail", r.text)
                except Exception:
                    detail = r.text
                print(f"resume-blocked failed: {detail}", file=sys.stderr)
                return 1

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
