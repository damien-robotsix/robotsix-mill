"""Deliver stage: DELIVERABLE -> IN_REVIEW.

Push the ticket's branch to the configured forge and open a PR/MR
against ``FORGE_TARGET_BRANCH``. The forge adapter only does the API
call; this stage owns the git push (it has the workspace clone).

Anything that isn't success is BLOCKED-resumable (move back to
DELIVERABLE to retry) — never terminal FAILED, so a transient forge/
network problem doesn't lose the implemented branch. The PR URL is
recorded in history + an artifact.
"""

from __future__ import annotations

import logging
import subprocess

from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import github_token
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.deliver")


class DeliverStage(Stage):
    name = "deliver"
    input_state = State.DELIVERABLE
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings
        if s.forge_kind == "none":
            return Outcome(State.BLOCKED, "FORGE_KIND not configured")
        if not s.forge_remote_url:
            return Outcome(State.BLOCKED, "FORGE_REMOTE_URL not configured")
        try:
            token = github_token(s)  # PAT or minted App installation token
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        if not (repo_dir / ".git").exists() or not git_ops.branch_exists(
            repo_dir, branch
        ):
            return Outcome(
                State.BLOCKED,
                "no implemented branch to deliver (re-run implement)",
            )

        try:
            git_ops.push(repo_dir, branch, s.forge_remote_url, token)
        except subprocess.CalledProcessError as e:
            return Outcome(
                State.BLOCKED,
                f"push failed — resumable: {(e.stderr or '')[:300]}",
            )

        title = f"mill: {ticket.title} ({ticket.id})"
        body = (
            ws.read_description()[:8000]
            + f"\n\n---\nAutomated by robotsix-mill · ticket `{ticket.id}`"
        )
        try:
            url = get_forge(s).open_merge_request(
                source_branch=branch, title=title, body=body
            )
        except Exception as e:  # noqa: BLE001 — resumable, don't lose branch
            log.exception("%s: open PR failed", ticket.id)
            return Outcome(State.BLOCKED, f"open PR failed — resumable: {e}")

        (ws.artifacts_dir / "deliver.md").write_text(
            f"# Deliver (passed)\nbranch: {branch}\nPR: {url}\n",
            encoding="utf-8",
        )
        log.info("%s: delivered → %s", ticket.id, url)
        # PR opened — await human merge (the merge stage polls for it)
        return Outcome(State.IN_REVIEW, f"PR: {url}")
