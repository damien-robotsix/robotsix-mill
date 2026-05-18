"""Merge stage: IN_REVIEW -> DONE (merged) | BLOCKED (closed unmerged).

The PR is the review. This stage is re-run by the worker's lightweight
poll while the ticket sits in IN_REVIEW; it checks the forge:

- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- still open / not found yet / transient API error
                    -> no-op (return IN_REVIEW; the poll retries later)

Returning the *same* state is the worker's "leave it, re-poll" signal —
no history spam, no busy loop.
"""

from __future__ import annotations

import logging

from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import github_token
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.merge")


class MergeStage(Stage):
    name = "merge"
    input_state = State.IN_REVIEW
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings
        if s.forge_kind == "none" or not s.forge_remote_url:
            return Outcome(State.BLOCKED, "forge not configured")
        try:
            github_token(s)  # surfaces a clear config error early
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        try:
            pr = get_forge(s).pr_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.IN_REVIEW)  # no-op

        if pr is None:
            return Outcome(State.IN_REVIEW)  # not visible yet — re-poll
        if pr.get("merged"):
            ctx.service.workspace(ticket).artifacts_dir.joinpath(
                "merge.md"
            ).write_text(f"merged: {pr.get('url', '')}\n", encoding="utf-8")
            log.info("%s: PR merged → done", ticket.id)
            return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )
        return Outcome(State.IN_REVIEW)  # still open — re-poll
