"""ReviewRevisionMixin: review-revision handling for the merge stage.

Handles ADDRESSING_REVIEW and CHANGES_REQUESTED detection: runs the
review-revision agent, force-pushes, and bounds retries with a per-ticket
attempt counter.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...core.models import Ticket
from ...core.states import State
from ...forge import Forge

# _resolve_remote_url, github_token, load_memory, persist_memory
# are accessed through the _facade import inside method bodies
# (so monkeypatching merge_mod.<name> propagates).
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _REV_REV_COUNTER,
    _read_counter,
    _reconcile_with_remote_pr,
    _write_counter,
    log,
)


class ReviewRevisionMixin(_MergeStageBase):
    """Review revision: ADDRESSING_REVIEW execution and CHANGES_REQUESTED detection."""

    def _run_review_revision(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Execute the review-revision agent for a ticket in ADDRESSING_REVIEW."""
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        repo_dir = _facade._workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return Outcome(
                State.BLOCKED,
                "Review feedback received but workspace clone is missing; "
                "cannot implement changes. Re-run implement to recreate the clone.",
            )

        # Read the persisted review feedback artifact.
        artifact_dir = ctx.service.workspace(ticket).artifacts_dir
        feedback_path = artifact_dir / "review_feedback.json"
        if not feedback_path.exists():
            return Outcome(
                State.HUMAN_MR_APPROVAL,
                "review_feedback.json artifact missing — re-polling from human_mr_approval",
            )

        try:
            feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return Outcome(
                State.HUMAN_MR_APPROVAL,
                "review_feedback.json corrupted — re-polling from human_mr_approval",
            )

        comments = feedback.get("comments", [])
        pr_files = feedback.get("files", [])

        if not comments:
            return Outcome(State.HUMAN_MR_APPROVAL)

        # Build a formatted review-comments string for the agent.
        parts: list[str] = []
        for i, c in enumerate(comments):
            loc = ""
            if c.get("path"):
                loc = f" ({c['path']}"
                if c.get("line"):
                    loc += f":{c['line']}"
                loc += ")"
            parts.append(f"## Comment #{i + 1}{loc}\n\n{c.get('body', '')}")
        review_comments_text = "\n\n".join(parts)

        # Counter for attempt budgeting.
        counter_path = artifact_dir / _REV_REV_COUNTER
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.review_revision_max_attempts

        log.info(
            "%s: addressing review feedback — attempt %d/%d",
            ticket.id,
            attempt,
            max_attempts,
        )

        try:
            # review_revision is traced=False (like ci_fix), so wrap the
            # LLM agent in the ticket's Langfuse session.
            with _facade.tracing.start_ticket_root_span(ticket.id, "review_revision"):
                # Reconcile with remote PR branch first so the agent
                # sees any foreign commits.
                remote_url = _facade._resolve_remote_url(s, ctx.repo_config)
                token = _facade.github_token(s, repo_config=ctx.repo_config)
                blocked = _reconcile_with_remote_pr(
                    _facade, repo_dir, remote_url, branch, token, ticket.id
                )
                if blocked is not None:
                    return blocked

                review_revision_memory_path = s.memory_file_for(
                    "review_revision", ctx.memory_board_id(ticket)
                )
                memory_text = _facade.load_memory(review_revision_memory_path)
                result = _facade.run_review_revision_agent(
                    settings=s,
                    repo_dir=Path(repo_dir),
                    branch=branch,
                    review_comments=review_comments_text,
                    pr_files=pr_files,
                    memory=memory_text,
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    _facade.persist_memory(
                        review_revision_memory_path, result.updated_memory
                    )
        except Exception as e:  # noqa: BLE001
            log.exception("%s: review-revision agent crashed: %s", ticket.id, e)
            ok = False

        if ok:
            # Only force-push when the local HEAD differs from remote.
            try:
                local = _facade.git_ops.head_sha(repo_dir)
                remote = _facade.git_ops.remote_branch_sha(repo_dir, branch)
            except Exception:  # noqa: BLE001
                local, remote = None, "force-push"

            if local is not None and remote == local:
                # Nothing to push — the agent made no changes.
                if attempt < max_attempts:
                    _write_counter(counter_path, attempt)
                    log.info(
                        "%s: review-revision no-op (remote already current) — "
                        "retry %d/%d",
                        ticket.id,
                        attempt,
                        max_attempts,
                    )
                    return Outcome(State.ADDRESSING_REVIEW)
                _write_counter(counter_path, 0)
                return Outcome(
                    State.BLOCKED,
                    "review-revision agent succeeded but made no changes "
                    f"after {max_attempts} attempt(s)",
                )

            try:
                # Per-repo remote + token (see the rebase push for why the
                # global forge_remote_url/tokenless mint break non-mill boards).
                # Use push_with_lease so a concurrent human push is never
                # silently overwritten.
                _facade.git_ops.push_with_lease(
                    Path(repo_dir),
                    branch=branch,
                    remote_url=_facade._resolve_remote_url(s, ctx.repo_config),
                    token=_facade.github_token(s, repo_config=ctx.repo_config),
                )
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "%s: force-push after review-revision failed: %s", ticket.id, e
                )
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"review revision succeeded but force-push failed: {e}",
                )

            _write_counter(counter_path, 0)
            log.info("%s: review feedback addressed, branch force-pushed", ticket.id)
            return Outcome(State.HUMAN_MR_APPROVAL)

        # Agent failed.
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: review-revision attempt %d/%d failed — retrying next poll",
                ticket.id,
                attempt,
                max_attempts,
            )
            return Outcome(State.ADDRESSING_REVIEW)

        _write_counter(counter_path, 0)
        return Outcome(
            State.BLOCKED,
            f"review revision failed after {max_attempts} attempt(s) — "
            "manual intervention required. "
            "Resume-blocked to retry from human_mr_approval.",
        )

    def _review_changes_requested_outcome(
        self,
        ticket: Ticket,
        ctx: StageContext,
        *,
        branch: str,
        forge: Forge,
        pr_head_sha: str = "",
    ) -> Outcome | None:
        """Return ``Outcome(ADDRESSING_REVIEW, ...)`` when the forge reports a
        CHANGES_REQUESTED review with at least one comment for ``branch``;
        else ``None``. Guarded by ``ctx.settings.review_feedback_enabled``.
        Persists ``artifacts/review_feedback.json`` before routing.

        Transient-tolerant: a ``pr_review_status`` exception logs a warning
        and is treated as ``None`` (no gate this poll). A CHANGES_REQUESTED
        review with an empty comment list AND an empty body is a no-op
        (log + return ``None``); if the body is non-empty, one comment is
        synthesized from it so the review still routes to ADDRESSING_REVIEW.

        *pr_head_sha* guards against stale-verdict replay: when non-empty,
        it is compared against the review's ``commit_id``.  When they
        differ the PR has been updated since the review was cast — the old
        verdict is discarded rather than replayed as a fresh rejection.
        """
        if not ctx.settings.review_feedback_enabled:
            # Even when review-feedback handling is disabled, a stale
            # CHANGES_REQUESTED forge review must be detected and
            # dismissed so it cannot block an approved MR from
            # auto-merging on a subsequent poll.  We still query the
            # forge for the review status, but only to check for (and
            # dismiss) stale artifacts — we never route to
            # ADDRESSING_REVIEW when the feature is off.
            try:
                review_status = forge.pr_review_status(source_branch=branch)
            except Exception:
                return None
            if (
                review_status is None
                or review_status.get("state") != "CHANGES_REQUESTED"
            ):
                return None
            # Stale review guard: dismiss CHANGES_REQUESTED reviews whose
            # target commit no longer matches the PR head.
            if pr_head_sha and review_status:
                review_commit_id = review_status.get("commit_id", "")
                if review_commit_id and pr_head_sha != review_commit_id:
                    log.info(
                        "%s: dismissing stale CHANGES_REQUESTED review "
                        "(review commit %.8s != PR head %.8s, "
                        "review_feedback_enabled=False)",
                        ticket.id,
                        review_commit_id,
                        pr_head_sha,
                    )
                    rid = review_status.get("review_id")
                    if rid is not None:
                        try:
                            forge.dismiss_review(source_branch=branch, review_id=rid)
                        except Exception:
                            log.warning(
                                "%s: failed to dismiss stale review %s",
                                ticket.id,
                                rid,
                            )
                    return None
            return None
        try:
            review_status = forge.pr_review_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: pr_review_status failed (retry): %s", ticket.id, e)
            return None

        if review_status is None or review_status.get("state") != "CHANGES_REQUESTED":
            return None

        # --- stale review guard ---
        # When the review was cast against a different commit than the
        # PR's current head, the diff has changed — the old verdict does
        # not apply.  Discard the stale rejection so the pipeline can
        # re-evaluate the updated PR from scratch rather than replaying
        # the old REQUEST_CHANGES.
        if pr_head_sha and review_status:
            review_commit_id = review_status.get("commit_id", "")
            if review_commit_id and pr_head_sha != review_commit_id:
                log.info(
                    "%s: CHANGES_REQUESTED review is stale "
                    "(review commit %.8s != PR head %.8s) — dismissing",
                    ticket.id,
                    review_commit_id,
                    pr_head_sha,
                )
                rid = review_status.get("review_id")
                if rid is not None:
                    try:
                        forge.dismiss_review(source_branch=branch, review_id=rid)
                    except Exception:
                        log.warning(
                            "%s: failed to dismiss stale review %s",
                            ticket.id,
                            rid,
                        )
                return None

        comments = review_status.get("comments", [])
        if not comments:
            body = (review_status.get("body") or "").strip()
            if not body:
                # CHANGES_REQUESTED with neither comments nor a review body —
                # nothing actionable to hand the revision agent. Treat as a
                # no-op so an auto-merge poll proceeds.
                log.info(
                    "%s: changes requested with empty body — treating as no-op",
                    ticket.id,
                )
                return None
            # EMPTY comments list but a non-empty review body — still
            # actionable: a human blocked the merge. Synthesize ONE comment
            # from the review body (path='', line=None) so the revision agent
            # has something to act on, rather than silently dropping it.
            review_status["comments"] = comments = [
                {
                    "body": body,
                    "path": "",
                    "line": None,
                    "review_state": "CHANGES_REQUESTED",
                }
            ]

        # Persist the review comments as an artifact so the agent can
        # read them even if the forge becomes unreachable on the next poll.
        artifact_dir = ctx.service.workspace(ticket).artifacts_dir
        review_json = json.dumps(review_status, indent=2)
        artifact_dir.joinpath("review_feedback.json").write_text(
            review_json, encoding="utf-8"
        )
        log.info(
            "%s: human requested changes (%d comments) → ADDRESSING_REVIEW",
            ticket.id,
            len(comments),
        )
        return Outcome(
            State.ADDRESSING_REVIEW,
            f"Reviewer requested changes with {len(comments)} comment(s)",
        )
