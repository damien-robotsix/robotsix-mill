"""Admin / maintenance subcommands: audit, health, copy-paste, etc.

These commands share a common dispatch pattern via ``_RUNNERS`` and
``_run_and_print`` - they dynamically import a runner module, call its
entry point, and format the result (JSON or human-readable).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from ..config import Settings
from ..config import get_repos_config
from ..runtime.tracing import make_session_id

_RUNNERS: dict[str, dict[str, str]] = {
    "audit": {
        "module": "runners.periodic_runner",
        "function": "run_audit_pass",
        "label": "Audit pass",
        "format": "memory_drafts",
    },
    "health": {
        "module": "runners.periodic_runner",
        "function": "run_health_pass",
        "label": "Health pass",
        "format": "memory_drafts",
    },
    "agent-check": {
        "module": "runners.periodic_runner",
        "function": "run_agent_check_pass",
        "label": "Agent-check pass",
        "format": "memory_drafts",
    },
    "test-gap": {
        "module": "runners.periodic_runner",
        "function": "run_test_gap_pass",
        "label": "Test-gap pass",
        "format": "memory_drafts",
    },
    "config-sync": {
        "module": "runners.periodic_runner",
        "function": "run_config_sync_pass",
        "label": "Config-sync pass",
        "format": "memory_drafts",
    },
    "member-sync": {
        "module": "runners.member_sync_runner",
        "function": "run_member_sync_pass",
        "label": "Member-sync pass",
        "format": "member_sync",
    },
    "trace-health": {
        "module": "runners.trace_health_runner",
        "function": "run_trace_health_check",
        "label": "Trace-health check",
        "format": "trace_health",
    },
    "trace-review": {
        "module": "runners.trace_review_runner",
        "function": "run_trace_review_pass",
        "label": "Trace-review pass",
        "format": "trace_review",
    },
    "langfuse-cleanup": {
        "module": "runners.langfuse_cleanup_runner",
        "function": "run_langfuse_cleanup_pass",
        "label": "Langfuse cleanup pass",
        "format": "langfuse_cleanup",
    },
    "bc-check": {
        "module": "runners.periodic_runner",
        "function": "run_bc_check_pass",
        "label": "BC-check pass",
        "format": "memory_drafts",
    },
    "completeness-check": {
        "module": "runners.periodic_runner",
        "function": "run_completeness_check_pass",
        "label": "Completeness-check pass",
        "format": "memory_drafts",
    },
    "run-health": {
        "module": "runners.run_health_runner",
        "function": "run_run_health_pass",
        "label": "Run-health pass",
        "format": "memory_notes",
    },
    "diagnostic": {
        "module": "runners.diagnostic_runner",
        "function": "run_diagnostic_pass",
        "label": "Diagnostic pass",
        "format": "drafts",
    },
    "survey": {
        "module": "runners.periodic_runner",
        "function": "run_survey_pass",
        "label": "Survey pass",
        "format": "memory_drafts",
    },
    "copy-paste": {
        "module": "runners.periodic_runner",
        "function": "run_copy_paste_pass",
        "label": "Copy-paste pass",
        "format": "memory_drafts",
    },
    "state-sync": {
        "module": "runners.periodic_runner",
        "function": "run_state_sync_pass",
        "label": "State-sync pass",
        "format": "memory_drafts",
    },
    "env-doc-sync": {
        "module": "runners.periodic_runner",
        "function": "run_env_doc_sync_pass",
        "label": "Env-doc-sync pass",
        "format": "memory_drafts",
    },
    "frontend-sync": {
        "module": "runners.periodic_runner",
        "function": "run_frontend_sync_pass",
        "label": "Frontend-sync pass",
        "format": "memory_drafts",
    },
    "forge-parity": {
        "module": "runners.periodic_runner",
        "function": "run_forge_parity_pass",
        "label": "Forge-parity pass",
        "format": "memory_drafts",
    },
    "module-curator": {
        "module": "runners.periodic_runner",
        "function": "run_module_curator_pass",
        "label": "Module-curator pass",
        "format": "memory_drafts",
    },
    "security-posture": {
        "module": "runners.periodic_runner",
        "function": "run_security_posture_pass",
        "label": "Security-posture pass",
        "format": "memory_drafts",
    },
    "roadmap-sync": {
        "module": "runners.roadmap_sync_runner",
        "function": "run_roadmap_sync_pass",
        "label": "Roadmap-sync pass",
        "format": "roadmap_sync",
    },
    "triage-boilerplate": {
        "module": "runners.periodic_runner",
        "function": "run_triage_boilerplate_pass",
        "label": "Triage-boilerplate pass",
        "format": "memory_drafts",
    },
    "verify": {
        "module": "runners.verify_runner",
        "function": "run_verify_pass",
        "label": "Verify",
        "format": "verify",
    },
    "meta": {
        "module": "meta.runner",
        "function": "run_meta_pass",
        "label": "Meta pass",
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
            session_id = make_session_id(cmd)
            ticket_id = getattr(args, "ticket_id", None)
            result = func(session_id=session_id, ticket_id=ticket_id)
        elif cmd == "langfuse-cleanup":
            settings = Settings()
            result = func(
                settings=settings,
                repo_config=None,
                max_traces=settings.langfuse_cleanup_max_traces,
            )
        elif cmd == "member-sync":
            session_id = make_session_id(cmd)
            repos = get_repos_config()
            repo_id = getattr(args, "repo_id", None)
            if repo_id is None:
                if len(repos.repos) == 1:
                    rc = next(iter(repos.repos.values()))
                else:
                    print(
                        "member-sync: --repo-id is required (multiple repos "
                        f"configured). Known repos: {sorted(repos.repos.keys())}",
                        file=sys.stderr,
                    )
                    return 2
            elif repo_id not in repos.repos:
                print(
                    f"member-sync: unknown repo '{repo_id}'. "
                    f"Known repos: {sorted(repos.repos.keys())}",
                    file=sys.stderr,
                )
                return 2
            else:
                rc = repos.repos[repo_id]
            result = func(session_id=session_id, repo_config=rc)
        elif cmd == "trace-review":
            session_id = make_session_id(cmd)
            repos = get_repos_config()
            repo_id = getattr(args, "repo_id", None)
            if repo_id is None:
                if len(repos.repos) == 1:
                    rc = next(iter(repos.repos.values()))
                else:
                    print(
                        "trace-review: --repo-id is required (multiple repos "
                        f"configured). Known repos: {sorted(repos.repos.keys())}",
                        file=sys.stderr,
                    )
                    return 2
            elif repo_id not in repos.repos:
                print(
                    f"trace-review: unknown repo '{repo_id}'. "
                    f"Known repos: {sorted(repos.repos.keys())}",
                    file=sys.stderr,
                )
                return 2
            else:
                rc = repos.repos[repo_id]
            result = func(session_id=session_id, repo_config=rc)
        elif cmd == "roadmap-sync":
            session_id = make_session_id(cmd)
            repos = get_repos_config()
            repo_id = getattr(args, "repo_id", None)
            if repo_id is None:
                if len(repos.repos) == 1:
                    rc = next(iter(repos.repos.values()))
                else:
                    print(
                        "roadmap-sync: --repo-id is required (multiple repos "
                        f"configured). Known repos: {sorted(repos.repos.keys())}",
                        file=sys.stderr,
                    )
                    return 2
            elif repo_id not in repos.repos:
                print(
                    f"roadmap-sync: unknown repo '{repo_id}'. "
                    f"Known repos: {sorted(repos.repos.keys())}",
                    file=sys.stderr,
                )
                return 2
            else:
                rc = repos.repos[repo_id]
            result = func(session_id=session_id, repo_config=rc)
        elif cmd == "meta":
            session_id = make_session_id(cmd)
            result = func(session_id=session_id)
            # Combine the three draft lists for the memory_drafts format handler.
            result.drafts_created = (
                result.extraction_drafts_created
                + result.alignment_drafts_created
                + result.todo_drafts_created
            )
        else:
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
                        "name_missing_count": result.name_missing_count,
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
        elif entry["format"] == "member_sync":
            print(
                json.dumps(
                    {
                        "added": result.added,
                        "updated": result.updated,
                        "flagged_for_removal": result.flagged_for_removal,
                        "filed_tickets": result.filed_tickets,
                        "skipped": result.skipped,
                    },
                    indent=2,
                )
            )
        elif entry["format"] == "trace_review":
            print(
                json.dumps(
                    {
                        "summary": result.summary,
                        "drafts_created": result.drafts_created,
                        "traces_scanned": result.traces_scanned,
                        "traces_flagged": result.traces_flagged,
                        "window_start": result.window_start,
                        "window_end": result.window_end,
                    },
                    indent=2,
                )
            )
        elif entry["format"] == "roadmap_sync":
            print(
                json.dumps(
                    {
                        "summary": result.summary,
                        "created": result.created,
                        "updated": result.updated,
                        "skipped": result.skipped,
                        "pr_url": result.pr_url,
                        "session_id": result.session_id,
                    },
                    indent=2,
                )
            )
        elif entry["format"] == "drafts":
            print(
                json.dumps(
                    {
                        "summary": result.summary,
                        "tickets_created": result.drafts_created,
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
                print("Draft ticket created for trace orphans.")
            else:
                print("No alert needed.")
            print(
                f"Unsessoned: {result.unsessioned_count}, "
                f"unnamed: {result.name_missing_count} / "
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
        elif entry["format"] == "member_sync":
            print("Member-sync pass complete.")
            print(
                f"Added: {len(result.added)} | "
                f"Updated: {len(result.updated)} | "
                f"Flagged for removal: {len(result.flagged_for_removal)} | "
                f"Skipped: {len(result.skipped)}"
            )
            if result.added:
                print(f"  Added: {', '.join(result.added)}")
            if result.updated:
                print(f"  Updated: {', '.join(result.updated)}")
            if result.flagged_for_removal:
                print(f"  Flagged: {', '.join(result.flagged_for_removal)}")
            if result.filed_tickets:
                for rid, tid in result.filed_tickets.items():
                    print(f"  Build-out ticket {tid} filed for {rid}")
        elif entry["format"] == "trace_review":
            print(f"{entry['label']} complete.")
            print(
                f"Traces scanned: {result.traces_scanned} | "
                f"flagged: {result.traces_flagged} | "
                f"drafts: {len(result.drafts_created)}"
            )
            print(f"Window: {result.window_start} → {result.window_end}")
            if result.drafts_created:
                print(f"Draft tickets created: {len(result.drafts_created)}")
                for d in result.drafts_created:
                    print(f"  - {d['id']}: {d.get('title', '')}")
            else:
                print("No new draft tickets created.")
        elif entry["format"] == "roadmap_sync":
            print(f"{entry['label']} complete.")
            print(
                f"Created: {len(result.created)} | "
                f"Updated: {len(result.updated)} | "
                f"Skipped: {len(result.skipped)}"
            )
            if result.pr_url:
                print(f"PR: {result.pr_url}")
            if result.created:
                print("Created:")
                for item in result.created:
                    print(f"  - {item.get('id', '?')}: {item.get('title', '')}")
            if result.updated:
                print("Updated:")
                for item in result.updated:
                    fields = item.get("fields", [])
                    print(
                        f"  - {item.get('id', '?')}: {item.get('title', '')}"
                        f" ({', '.join(fields)})"
                    )
            if result.skipped:
                print("Skipped:")
                for item in result.skipped:
                    print(f"  - {item.get('title', '?')}: {item.get('reason', '')}")
            if not result.created and not result.updated and not result.skipped:
                print("No changes.")
        elif entry["format"] == "drafts":
            print(f"{entry['label']} complete.")
            print(result.summary)
            if result.drafts_created:
                print(f"Draft tickets created: {len(result.drafts_created)}")
                for d in result.drafts_created:
                    print(f"  - {d['id']}: {d.get('title', '')}")
            else:
                print("No new draft tickets created.")
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
