"""Output formatting for CLI subcommand results."""

from __future__ import annotations

import argparse
import json
from typing import Any


def print_result(entry: dict[str, str], result: Any, args: argparse.Namespace) -> None:
    """Print a runner result in either JSON or human-readable format."""
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
