"""Argument parser for the ``robotsix-mill`` CLI.

The parser is separated from ``main()`` so that completion-script
generators (e.g. ``scripts/gen_completions.py``) can introspect it
without side effects.
"""

from __future__ import annotations

import argparse

from ..core.states import State


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
