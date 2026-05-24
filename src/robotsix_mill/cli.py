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
    "env-sync": {
        "module": "env_sync_runner",
        "function": "run_env_sync_pass",
        "label": "Env-sync pass",
        "format": "memory_drafts",
    },
    "trace-health": {
        "module": "trace_health_runner",
        "function": "run_trace_health_check",
        "label": "Trace-health check",
        "format": "trace_health",
    },
    "bc-check": {
        "module": "bc_check_runner",
        "function": "run_bc_check_pass",
        "label": "BC-check pass",
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
        result = func()
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

    sub.add_parser("serve", help="run the API + event-driven worker")

    p_ticket = sub.add_parser("ticket", help="ticket operations")
    tsub = p_ticket.add_subparsers(dest="tcmd", required=True)

    p_new = tsub.add_parser("new", help="emit a ticket (worker picks it up)")
    p_new.add_argument("--title", required=True)
    p_new.add_argument(
        "--description-file", help="file with the body; '-' reads stdin"
    )

    p_list = tsub.add_parser("list", help="list tickets")
    p_list.add_argument("--state", choices=[s.value for s in State])

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

    # --- env-sync command ---
    p_env_sync = sub.add_parser(
        "env-sync", help="run an env-sync config/docs drift detection pass"
    )
    p_env_sync.add_argument(
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
        import uvicorn

        from .runtime.api import create_app

        uvicorn.run(
            create_app(settings), host=settings.api_host, port=settings.api_port
        )
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
            r = c.post(
                "/tickets", json={"title": args.title, "description": body}
            )
            r.raise_for_status()
            print(r.json()["id"])
            return 0
        if args.tcmd == "list":
            params = {"state": args.state} if args.state else {}
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
