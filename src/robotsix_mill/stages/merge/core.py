"""The :class:`MergeStage` coordinator.

Assembles the responsibility-focused mixins
(:class:`~.multi_repo.MultiRepoMixin`,
:class:`~.ci_poll.CIPollMixin`,
:class:`~.rebase.RebaseMixin`,
:class:`~.review_revision.ReviewRevisionMixin`) into the public
``Stage`` subclass via multiple inheritance.

This module is the only one in the package that imports the mixins; the
mixins never import each other or ``core`` (cross-responsibility calls
go through ``cls``/``self`` on the assembled class), so the package
import graph is a strict acyclic DAG.
"""

from __future__ import annotations

from ...core.models import Ticket
from ...core.states import State
from ...forge import get_forge
from ...forge.auth import github_token
from ..base import Outcome, Stage, StageContext
from ._shared import (
    _MERGE_REASON,
    _load_pr_urls,
    _read_reason,
    _write_reason,
    log,
)
from .ci_poll import CIPollMixin
from .multi_repo import MultiRepoMixin
from .rebase import RebaseMixin
from .review_revision import ReviewRevisionMixin


class MergeStage(
    MultiRepoMixin,
    CIPollMixin,
    RebaseMixin,
    ReviewRevisionMixin,
    Stage,
):
    """Orchestrate the merge pipeline: poll CI, rebase, address review feedback, and auto-merge when green."""

    name = "merge"
    input_state = State.HUMAN_MR_APPROVAL
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Drive a ticket through the merge pipeline: poll CI / mergeability, dispatch to rebase or review-revision handlers based on the current state, and auto-merge when all gates are green."""
        s = ctx.settings
        if s.forge_kind == "none":
            return Outcome(State.BLOCKED, "forge not configured")
        try:
            github_token(s)  # surfaces a clear config error early
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        # Multi-repo mode (meta-board tickets). When the deliver stage
        # wrote ``pr_urls.json`` we drive aggregation across every
        # touched repo via the dedicated aggregator. Single-repo
        # tickets fall through to the existing dispatch unchanged.
        ws = ctx.service.workspace(ticket)
        try:
            pr_entries = _load_pr_urls(ws.artifacts_dir)
        except ValueError as e:
            return Outcome(
                State.BLOCKED,
                f"pr_urls.json corrupted — resumable: {e}",
            )
        if pr_entries is not None:
            # An empty list is unreachable today — deliver routes to
            # DONE before writing the file when every repo is skipped.
            # Treat the impossible-empty case as a corrupt manifest.
            if not pr_entries:
                return Outcome(
                    State.BLOCKED,
                    "pr_urls.json corrupted — resumable: empty manifest",
                )
            return self._run_multi_repo(ticket, ctx, pr_entries)

        # IMPLEMENT_COMPLETE path: poll gates (CI + mergeability).
        if ticket.state is State.IMPLEMENT_COMPLETE:
            return self._poll_implement_complete(ticket, ctx)

        # REBASING path: skip PR status, go straight to rebase execution.
        if ticket.state is State.REBASING:
            return self._run_rebase(ticket, ctx)

        # ADDRESSING_REVIEW path: run review-revision agent, force-push.
        if ticket.state is State.ADDRESSING_REVIEW:
            return self._run_review_revision(ticket, ctx)

        # WAITING_AUTO_MERGE path: re-poll CI, try auto-merge when green.
        if ticket.state is State.WAITING_AUTO_MERGE:
            return self._poll_waiting_auto_merge(ticket, ctx)

        # HUMAN_MR_APPROVAL path: poll PR status.
        return self._handle_human_mr_approval(ticket, ctx)

    def _maybe_comment(self, ticket: Ticket, ctx: StageContext, reason: str) -> None:
        """Append a de-duplicated step event naming the auto-merge blocking condition.

        Reads ``merge_reason.txt`` from the workspace; skips emission
        if the stored reason matches *reason* exactly. Otherwise emits
        a same-state history event, then persists the new reason.

        Pre-v1 this used add_comment so the merge agent's reason
        appeared in the comments pane; that polluted comments with
        agent conclusions. The reason now lands in history alongside
        every other agent step.
        """
        reason_path = ctx.service.workspace(ticket).artifacts_dir / _MERGE_REASON
        stored = _read_reason(reason_path)
        if stored == reason:
            return  # already emitted — de-dupe
        ctx.service.add_step_event(ticket.id, f"merge: {reason}")
        _write_reason(reason_path, reason)

    def _cleanup_branch_on_done(self, ticket, ctx, branch: str) -> None:
        """Best-effort: delete the merged head branch on the forge.
        Gated by settings.delete_branch_on_merge. Never raises — a
        cleanup failure must not block the DONE transition."""
        if not ctx.settings.delete_branch_on_merge:
            return
        try:
            get_forge(ctx.settings, repo_config=ctx.repo_config).delete_branch(
                branch=branch
            )
        except Exception as e:  # noqa: BLE001 — best-effort cleanup, never fatal
            log.warning("%s: branch cleanup failed for %s: %s", ticket.id, branch, e)
