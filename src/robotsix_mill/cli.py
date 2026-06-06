"""``robotsix-mill`` CLI — a thin HTTP client over the service.

    robotsix-mill serve                       # run the API + worker
    robotsix-mill ticket new --title T [--description-file F | -]
    robotsix-mill ticket list [--state S]
    robotsix-mill ticket show <id>
    robotsix-mill ticket approve <id>
    robotsix-mill ticket resume-blocked <id>
    robotsix-mill epic new --title T [--description-file F | -]
    robotsix-mill inquire --title T [--description-file F | -]
    robotsix-mill action list --repo-id X [--status S]
    robotsix-mill action approve <id> --repo-id X
    robotsix-mill action reject <id> --repo-id X
    robotsix-mill audit                        # run an audit pass
    robotsix-mill trace-health                 # run a trace-health check
    robotsix-mill health                        # run a health pass
    robotsix-mill board-cleanup                # run a board-cleanup pass
    robotsix-mill copy-paste                   # run a copy-paste detection pass

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
        "module": "runners.audit_runner",
        "function": "run_audit_pass",
        "label": "Audit pass",
        "format": "memory_drafts",
    },
    "health": {
        "module": "runners.health_runner",
        "function": "run_health_pass",
        "label": "Health pass",
        "format": "memory_drafts",
    },
    "agent-check": {
        "module": "runners.agent_check_runner",
        "function": "run_agent_check_pass",
        "label": "Agent-check pass",
        "format": "memory_drafts",
    },
    "test-gap": {
        "module": "runners.test_gap_runner",
        "function": "run_test_gap_pass",
        "label": "Test-gap pass",
        "format": "memory_drafts",
    },
    "config-sync": {
        "module": "runners.config_sync_runner",
        "function": "run_config_sync_pass",
        "label": "Config-sync pass",
        "format": "memory_drafts",
    },
    "trace-health": {
        "module": "runners.trace_health_runner",
        "function": "run_trace_health_check",
        "label": "Trace-health check",
        "format": "trace_health",
    },
    "langfuse-cleanup": {
        "module": "runners.langfuse_cleanup_runner",
        "function": "run_langfuse_cleanup_pass",
        "label": "Langfuse cleanup pass",
        "format": "langfuse_cleanup",
    },
    "bc-check": {
        "module": "runners.bc_check_runner",
        "function": "run_bc_check_pass",
        "label": "BC-check pass",
        "format": "memory_drafts",
    },
    "completeness-check": {
        "module": "runners.completeness_check_runner",
        "function": "run_completeness_check_pass",
        "label": "Completeness-check pass",
        "format": "memory_drafts",
    },
    "cost-reconciliation": {
        "module": "runners.cost_reconciliation_runner",
        "function": "run_cost_reconciliation_pass",
        "label": "Cost-reconciliation pass",
        "format": "memory_drafts",
    },
    "survey": {
        "module": "runners.survey_runner",
        "function": "run_survey_pass",
        "label": "Survey pass",
        "format": "memory_drafts",
    },
    "copy-paste": {
        "module": "runners.copy_paste_runner",
        "function": "run_copy_paste_pass",
        "label": "Copy-paste pass",
        "format": "memory_drafts",
    },
    "module-curator": {
        "module": "runners.module_curator_runner",
        "function": "run_module_curator_pass",
        "label": "Module-curator pass",
        "format": "memory_drafts",
    },
    "verify": {
        "module": "runners.verify_runner",
        "function": "run_verify_pass",
        "label": "Verify",
        "format": "verify",
    },
    "board-cleanup": {
        "module": "runners.periodic_runner",
        "function": "run_board_cleanup_pass",
        "label": "Board-cleanup pass",
        "format": "memory_drafts",
    },
}


def _run_and_print(cmd: str, args: argparse.Namespace) -> int:
    """Dynamically import and run a subcommand's runner, then print results."""
    entry = _RUNNERS[cmd]
    mod = importlib.import_module(f".{entry['module']}", package="robotsix_mill")
    func = getattr(mod, entry["function"])

    try:
        if cmd == "trace-health":
            result = func()
        elif cmd == "verify":
            from .runtime.tracing import make_session_id

            session_id = make_session_id(cmd)
            ticket_id = getattr(args, "ticket_id", None)
            result = func(session_id=session_id, ticket_id=ticket_id)
        elif cmd == "langfuse-cleanup":
            from .config import Settings

            settings = Settings()
            result = func(
                settings=settings,
                repo_config=None,
                max_traces=settings.langfuse_cleanup_max_traces,
            )
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
        elif cmd == "board-cleanup":
            from .runtime.tracing import make_session_id
            from .config import Settings, get_repos_config

            session_id = make_session_id(cmd)
            repos = get_repos_config()
            repo_id = getattr(args, "repo_id", None)
            if repo_id is None:
                if len(repos.repos) == 1:
                    rc = next(iter(repos.repos.values()))
                else:
                    print(
                        "board-cleanup: --repo-id is required (multiple repos "
                        f"configured). Known repos: {sorted(repos.repos.keys())}",
                        file=sys.stderr,
                    )
                    return 2
            elif repo_id not in repos.repos:
                print(
                    f"board-cleanup: unknown repo '{repo_id}'. "
                    f"Known repos: {sorted(repos.repos.keys())}",
                    file=sys.stderr,
                )
                return 2
            else:
                rc = repos.repos[repo_id]
            result = func(session_id=session_id, repo_config=rc, settings=Settings())
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
        elif entry["format"] == "verify":
            print(
                json.dumps(
                    {
                        "total_events": result.total_events,
                        "tickets_verified": result.tickets_verified,
                        "breaks": result.breaks,
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
        elif entry["format"] == "verify":
            print("Verify complete.")
            print(f"Total events scanned: {result.total_events}")
            print(f"Tickets verified: {result.tickets_verified}")
            if result.breaks:
                print(f"Integrity breaks found: {len(result.breaks)}")
                for b in result.breaks:
                    print(
                        f"  - event {b['event_id']} (ticket {b['ticket_id']}): "
                        f"{b['field']} mismatch"
                    )
            else:
                print("No integrity breaks found.")
        else:
            print(f"{entry['label']} complete.")
            print(f"Memory updated: {len(result.updated_memory)} chars")
            if result.drafts_created:
                print(f"Draft tickets created: {len(result.drafts_created)}")
                for d in result.drafts_created:
                    print(f"  - {d['id']}: {d['title']}")
            else:
                print("No new draft tickets created.")
    return 0


def _serve(args: argparse.Namespace, settings: Settings) -> int:
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
    except ValueError, OSError:
        pass

    import uvicorn

    from .runtime.api import create_app
    from .config import get_repos_config
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


def _repos_list(args: argparse.Namespace, settings: Settings) -> int:
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
        return args.repo_id

    from .config import get_repos_config
    from .config_loader import ConfigError as _ConfigError

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


def _inquire(args: argparse.Namespace, settings: Settings) -> int:
    body = _read_body_from_args(args)
    with _client(settings) as c:
        r = c.post(
            "/tickets",
            json={"title": args.title, "description": body, "kind": "inquiry"},
        )
        r.raise_for_status()
        print(r.json()["id"])
    return 0


def _ticket_new(args: argparse.Namespace, settings: Settings) -> int:
    body = _read_body_from_args(args)
    repo_id = _resolve_repo_id(args)
    if repo_id is None:
        return 2
    with _client(settings) as c:
        r = c.post(
            "/tickets",
            json={"title": args.title, "description": body, "repo_id": repo_id},
        )
        r.raise_for_status()
        print(r.json()["id"])
    return 0


def _epic_new(args: argparse.Namespace, settings: Settings) -> int:
    body = _read_body_from_args(args)
    repo_id = _resolve_repo_id(args)
    if repo_id is None:
        return 2
    with _client(settings) as c:
        r = c.post(
            "/epics",
            json={"title": args.title, "description": body, "repo_id": repo_id},
        )
        r.raise_for_status()
        print(r.json()["id"])
    return 0


def _ticket_list(args: argparse.Namespace, settings: Settings) -> int:
    params: dict = {"state": args.state} if args.state else {}
    if args.repo_id:
        params["repo_id"] = args.repo_id
    with _client(settings) as c:
        r = c.get("/tickets", params=params)
        r.raise_for_status()
        for t in r.json():
            print(f"{t['id']}\t{t['state']}\t{t['title']}")
    return 0


def _ticket_show(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.get(f"/tickets/{args.id}")
        r.raise_for_status()
        print(r.json())
        h = c.get(f"/tickets/{args.id}/history")
        print("--- history ---")
        for e in h.json():
            print(f"{e['at']}\t{e['state']}\t{e.get('note')}")
    return 0


def _ticket_approve(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
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


def _ticket_resume_blocked(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
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


def _action_list(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.get(
            "/proposed-actions",
            params={"repo_id": args.repo_id, "status": args.status},
        )
        if not r.is_success:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"list failed: {detail}", file=sys.stderr)
            return 1
        for a in r.json():
            rationale = (a.get("rationale") or "")[:80]
            print(
                f"{a['id']}\t{a['source']}\t{a['action_type']}\t"
                f"{a['target_ticket_id']}\t{rationale}"
            )
    return 0


def _action_approve(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.post(
            f"/proposed-actions/{args.id}/approve",
            params={"repo_id": args.repo_id},
        )
        if r.is_success:
            data = r.json()
            print(f"action {data['id']} approved — now {data['status']}")
            return 0
        else:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"approve failed: {detail}", file=sys.stderr)
            return 1


def _action_reject(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.post(
            f"/proposed-actions/{args.id}/reject",
            params={"repo_id": args.repo_id},
        )
        if r.is_success:
            data = r.json()
            print(f"action {data['id']} rejected — now {data['status']}")
            return 0
        else:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"reject failed: {detail}", file=sys.stderr)
            return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for the robotsix-mill CLI.

    Available subcommands:

    * ``serve`` — run the API and event-driven worker
    * ``ticket new|list|show|approve|resume-blocked`` — ticket lifecycle
      operations
    * ``audit`` — run an audit pass and emit gap drafts
    * ``trace-health`` — check Langfuse for unsessioned traces
    * ``health`` — run a health pass and emit gap drafts
    * ``copy-paste`` — run a copy-paste / code-duplication detection pass

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
    p_new.add_argument("--description-file", help="file with the body; '-' reads stdin")
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
    p_audit = sub.add_parser("audit", help="run an audit pass and emit gap drafts")
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
        "cost-reconciliation",
        help="run an OpenRouter vs Langfuse cost drift detection pass",
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

    # --- module-curator command ---
    p_module_curator = sub.add_parser(
        "module-curator", help="run a module taxonomy drift detection pass"
    )
    p_module_curator.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )

    # --- board-cleanup command ---
    p_board_cleanup = sub.add_parser(
        "board-cleanup", help="run a board-cleanup pass and emit cleanup drafts"
    )
    p_board_cleanup.add_argument(
        "--json",
        action="store_true",
        help="output full JSON result (default: summary)",
    )
    p_board_cleanup.add_argument(
        "--repo-id",
        help="scope to a specific repo (required when multiple repos are configured)",
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

    # --- action command ---
    p_action = sub.add_parser("action", help="proposed action operations")
    asub = p_action.add_subparsers(dest="acmd", required=True)

    p_action_list = asub.add_parser("list", help="list pending proposed actions")
    p_action_list.add_argument("--repo-id", required=True)
    p_action_list.add_argument("--status", default="pending")

    p_action_approve = asub.add_parser(
        "approve", help="approve and execute a proposed action"
    )
    p_action_approve.add_argument("id", type=int)
    p_action_approve.add_argument("--repo-id", required=True)

    p_action_reject = asub.add_parser("reject", help="reject a proposed action")
    p_action_reject.add_argument("id", type=int)
    p_action_reject.add_argument("--repo-id", required=True)

    args = parser.parse_args(argv)
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
    if args.cmd == "action":
        if args.acmd == "list":
            return _action_list(args, settings)
        if args.acmd == "approve":
            return _action_approve(args, settings)
        if args.acmd == "reject":
            return _action_reject(args, settings)
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
