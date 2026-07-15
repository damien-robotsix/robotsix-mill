"""MultiRepoMixin: multi-repo orchestration for the merge stage.

Handles aggregation across multiple repos when ``pr_urls.json`` is present:
per-repo status polling, inline ci-fix and rebase recovery, and auto-merge.
"""

from __future__ import annotations

from pathlib import Path

from ...config import ConfigError, get_repo_config, target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...forge import get_forge

# _resolve_remote_url, github_token, load_memory, persist_memory
# are accessed through the _facade import inside method bodies
# (so monkeypatching merge_mod.<name> propagates).
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _ci_truly_green,
    _read_counter,
    _reconcile_with_remote_pr,
    _repo_config_for_entry,
    _write_counter,
    log,
)


class MultiRepoMixin(_MergeStageBase):
    """Multi-repo orchestration: aggregate per-repo PR status into one ticket-level outcome."""

    def _run_multi_repo(
        self,
        ticket: Ticket,
        ctx: StageContext,
        pr_entries: list[dict],
    ) -> Outcome:
        """Aggregate per-repo PR status into one ticket-level outcome, with
        per-repo auto-recovery.

        For every entry in ``pr_urls.json`` we resolve the per-repo
        :class:`RepoConfig` and query ``pr_status`` / ``check_status``.
        Per-repo statuses (``merged`` / ``open`` / ``closed_unmerged`` /
        ``conflicting`` / ``failing_ci`` / ``green`` / ``pending``) are then
        aggregated in priority order:

        * Any ``closed_unmerged`` -> BLOCKED.
        * Any ``conflicting`` -> run the rebase agent on ONE conflicting repo
          this poll (bounded by a per-repo attempt counter), force-push, and
          re-poll; exhausting the counter -> BLOCKED. A multi-repo ticket has a
          single state, so this recovery runs inline during the
          IMPLEMENT_COMPLETE poll rather than via the single-repo REBASING
          state.
        * Any ``failing_ci`` -> run the CI-fix agent on ONE failing repo this
          poll (bounded by a per-repo attempt counter), push, and re-poll;
          exhausting the counter -> BLOCKED. A multi-repo ticket has a single
          state, so this recovery runs inline during the IMPLEMENT_COMPLETE
          poll rather than via the single-repo FIXING_CI state.
        * All ``merged`` -> write ``merge.md`` and -> DONE.
        * All ``green`` (none pending) -> auto-merge the green PRs when the
          review gate marks the ticket eligible; the next poll then sees them
          merged and advances to DONE.
        * Otherwise (mix of green / pending) -> same-state no-op, re-poll.
        """
        s = ctx.settings

        statuses: list[dict] = []
        for entry in pr_entries:
            repo_id = entry.get("repo_id", "")
            branch = entry.get("branch", "")
            url = entry.get("url", "")
            base = {"repo_id": repo_id, "branch": branch, "url": url}

            try:
                rc = _repo_config_for_entry(entry)
            except ConfigError:
                return Outcome(
                    State.BLOCKED,
                    f"unknown repo_id '{repo_id}' in pr_urls.json — resumable",
                )

            try:
                pr = get_forge(s, repo_config=rc).pr_status(source_branch=branch)
            except Exception as e:  # noqa: BLE001 — transient: re-poll next cycle
                log.warning(
                    "%s: pr_status failed for %s (retry): %s", ticket.id, repo_id, e
                )
                statuses.append({**base, "status": "pending"})
                continue

            if pr is None and url:
                # Branch-keyed lookup came back empty. When the repo/org
                # has "Automatically delete head branches" enabled, GitHub
                # removes the head branch on merge and the ``head=`` filter
                # in the branch-keyed lookup returns nothing — so a merged
                # PR looks like it doesn't exist. Fall back to the
                # URL-keyed lookup (which resolves the PR by its recorded
                # url) so a merged PR is still recognised. Same transient-
                # exception discipline as ``pr_status``: log + treat as
                # pending for this poll.
                try:
                    pr = get_forge(s, repo_config=rc).pr_status_by_url(url=url)
                except Exception as e:  # noqa: BLE001 — transient: re-poll next cycle
                    log.warning(
                        "%s: pr_status_by_url failed for %s (retry): %s",
                        ticket.id,
                        repo_id,
                        e,
                    )
                    statuses.append({**base, "status": "pending"})
                    continue

            if pr is None:
                statuses.append({**base, "status": "pending"})
                continue
            base["url"] = pr.get("url", url)
            if pr.get("merged"):
                statuses.append({**base, "status": "merged"})
                continue
            if pr.get("state") == "closed":
                statuses.append({**base, "status": "closed_unmerged"})
                continue
            if pr.get("mergeable") is False:
                statuses.append({**base, "status": "conflicting"})
                continue

            try:
                ci = get_forge(s, repo_config=rc).check_status(source_branch=branch)
            except Exception as e:  # noqa: BLE001 — transient
                log.warning(
                    "%s: check_status failed for %s (retry): %s",
                    ticket.id,
                    repo_id,
                    e,
                )
                statuses.append({**base, "status": "pending"})
                continue

            conclusion = (ci or {}).get("conclusion")
            if conclusion == "failure":
                statuses.append({**base, "status": "failing_ci"})
            elif _ci_truly_green(conclusion, pr):
                # Reset per-repo ci-fix cycle counter when CI turns green.
                cycle_path = (
                    ctx.service.workspace(ticket).artifacts_dir
                    / f"ci_fix_{repo_id}_cycles.txt"
                )
                _write_counter(cycle_path, 0)
                statuses.append({**base, "status": "green"})
            else:
                statuses.append({**base, "status": "pending"})

        # --- Aggregate to a single ticket-level outcome in priority order. ---
        closed_unmerged = [r for r in statuses if r["status"] == "closed_unmerged"]
        if closed_unmerged:
            first = closed_unmerged[0]
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge in {first['repo_id']}: "
                f"{first['url']} — resumable",
            )

        conflicting = [r for r in statuses if r["status"] == "conflicting"]
        if conflicting:
            # Rebase one conflicting repo per poll; the rest re-check next cycle.
            return self._multi_repo_rebase(ticket, ctx, conflicting[0])

        failing = [r for r in statuses if r["status"] == "failing_ci"]
        if failing:
            # Fix one failing repo per poll; the rest re-check next cycle.
            return self._multi_repo_fix_ci(ticket, ctx, failing[0])

        if statuses and all(r["status"] == "merged" for r in statuses):
            lines = ["# Merge (multi-repo)", "repos:"]
            for r in statuses:
                lines.append(f"  - {r['repo_id']}: merged: {r['url']}")
            ctx.service.workspace(ticket).artifacts_dir.joinpath("merge.md").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            urls = ", ".join(r["url"] for r in statuses)
            log.info("%s: all %d PRs merged → done", ticket.id, len(statuses))
            return Outcome(
                State.DONE,
                f"all {len(statuses)} PRs merged: {urls}",
            )

        # No failures/conflicts/closed remain. If every non-merged repo is
        # green (nothing pending), auto-merge the green PRs (review-gated).
        green = [r for r in statuses if r["status"] == "green"]
        pending = [r for r in statuses if r["status"] == "pending"]
        if green and not pending:
            return self._multi_repo_auto_merge(ticket, ctx, green)

        # Mix of green / pending / merged — same-state no-op; re-poll.
        return Outcome(ticket.state)

    def _multi_repo_rebase(
        self, ticket: Ticket, ctx: StageContext, status: dict
    ) -> Outcome:
        """Run the rebase agent on one multi-repo PR that is conflicting.

        Mirrors the single-repo rebase path (:meth:`_handle_conflict` and
        friends) but inline (a multi-repo ticket has one state, so it cannot
        reuse the REBASING state cycle). Bounded by a per-repo attempt counter;
        exhausting the cap -> BLOCKED. Returns the ticket's current state
        (re-poll) while making progress.
        """
        s = ctx.settings
        from robotsix_mill.stages import merge as _facade

        repo_id = status["repo_id"]
        branch = status["branch"]
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repos" / repo_id
        if not (repo_dir / ".git").exists():
            return Outcome(
                State.BLOCKED,
                f"clone for {repo_id} missing — re-run implement",
            )
        try:
            rc = get_repo_config(repo_id)
        except ConfigError as e:
            return Outcome(
                State.BLOCKED, f"unknown repo_id '{repo_id}': {e} — resumable"
            )

        counter_path = ws.artifacts_dir / f"rebase_{repo_id}.count"
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.rebase_max_attempts
        if attempt > max_attempts:
            _write_counter(counter_path, 0)
            return Outcome(
                State.BLOCKED,
                f"rebase for {repo_id} failed after {max_attempts} attempt(s) — "
                "manual conflict resolution required",
            )

        target = target_branch_for(s, rc)
        log.info(
            "%s: multi-repo PR conflicting for %s — rebase attempt %d/%d onto %s",
            ticket.id,
            repo_id,
            attempt,
            max_attempts,
            target,
        )

        ok = False
        try:
            # Attribute the agent's cost/traces to the ticket's session, and
            # to the TARGET repo's Langfuse project, not an orphan trace.
            with _facade.tracing.start_ticket_root_span(
                ticket.id, "rebase", repo_config=rc
            ):
                remote_url = _facade._resolve_remote_url(s, rc)
                token = _facade.github_token(s, repo_config=rc)

                # Reconcile with remote PR branch first so the rebase
                # agent sees any foreign commits.
                blocked = _reconcile_with_remote_pr(
                    _facade, repo_dir, remote_url, branch, token, ticket.id, repo_id
                )
                if blocked is not None:
                    return blocked

                _facade.git_ops.fetch(
                    Path(repo_dir),
                    remote_url=remote_url,
                    token=token,
                    branch=target,
                )
                mem_path = s.memory_file_for("rebase", rc.board_id)
                result = _facade.run_rebase_agent(
                    settings=s,
                    repo_dir=str(repo_dir),
                    branch=branch,
                    target=target,
                    memory=_facade.load_memory(mem_path),
                    remote_url=remote_url,
                    token=token,
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    _facade.persist_memory(mem_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "%s: multi-repo rebase crashed for %s: %s", ticket.id, repo_id, e
            )
            ok = False

        if ok:
            # A rebase that produced no new commits (remote already current)
            # is a no-op: GitHub may still be recomputing mergeability, or the
            # base is stale. Count it (don't reset) so the attempt cap bounds
            # the loop rather than letting it spin.
            try:
                local = _facade.git_ops.head_sha(repo_dir)
                remote = _facade.git_ops.remote_branch_sha(repo_dir, branch)
            except Exception:  # noqa: BLE001 — be safe: assume changes
                local, remote = None, "force-push"
            if local is not None and remote == local:
                _write_counter(counter_path, attempt)
                log.info(
                    "%s: multi-repo rebase for %s made no changes (attempt %d/%d)",
                    ticket.id,
                    repo_id,
                    attempt,
                    max_attempts,
                )
                return Outcome(ticket.state)
            try:
                check = _facade.git_ops.post_push_check(
                    repo_dir,
                    branch=branch,
                    target=target_branch_for(s, rc),
                    remote_url=_facade._resolve_remote_url(s, rc),
                    token=_facade.github_token(s, repo_config=rc),
                )
                if check is _facade.git_ops.PostPushResult.PASS:
                    _write_counter(counter_path, 0)
                    log.info(
                        "%s: multi-repo rebase push verified for %s — re-poll",
                        ticket.id,
                        repo_id,
                    )
                    return Outcome(ticket.state)
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"rebase for {repo_id} post-check failed: {check}",
                )
            except Exception as e:  # noqa: BLE001
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"rebase for {repo_id} post-check error: {e}",
                )

        # Agent failed — record the attempt and re-poll.
        _write_counter(counter_path, attempt)
        log.warning(
            "%s: multi-repo rebase attempt %d/%d failed for %s — retrying next poll",
            ticket.id,
            attempt,
            max_attempts,
            repo_id,
        )
        return Outcome(ticket.state)

    def _multi_repo_auto_merge(
        self, ticket: Ticket, ctx: StageContext, green: list[dict]
    ) -> Outcome:
        """Auto-merge the green multi-repo PRs when the review gate marks the
        ticket eligible. Held in HUMAN_MR_APPROVAL when not eligible, so a human
        can merge instead (mirrors the single-repo gate path). Partial merges
        are fine — the next poll continues."""
        s = ctx.settings

        # --- Review feedback check (opt-in): if any repo's PR has a late
        # CHANGES_REQUESTED review, short-circuit to ADDRESSING_REVIEW before
        # merging any repo. ---
        for r in green:
            try:
                rc = get_repo_config(r["repo_id"])
            except ConfigError as e:
                return Outcome(
                    State.BLOCKED,
                    f"unknown repo_id '{r['repo_id']}': {e} — resumable",
                )
            review_outcome = self._review_changes_requested_outcome(
                ticket,
                ctx,
                branch=r["branch"],
                forge=get_forge(s, repo_config=rc),
            )
            if review_outcome is not None:
                return review_outcome

        eligible, reason = self._auto_merge_eligible(ticket, ctx)
        if not eligible:
            self._maybe_comment(
                ticket, ctx, f"multi-repo PRs green; auto-merge held: {reason}"
            )
            return Outcome(State.HUMAN_MR_APPROVAL, reason)

        for r in green:
            try:
                rc = get_repo_config(r["repo_id"])
                get_forge(s, repo_config=rc).merge_pr(source_branch=r["branch"])
                log.info(
                    "%s: multi-repo auto-merged %s: %s",
                    ticket.id,
                    r["repo_id"],
                    r["url"],
                )
                if s.delete_branch_on_merge:
                    try:
                        get_forge(s, repo_config=rc).delete_branch(branch=r["branch"])
                    except Exception as e:  # noqa: BLE001 — best-effort cleanup
                        log.warning(
                            "%s: branch cleanup failed for %s (%s): %s",
                            ticket.id,
                            r["repo_id"],
                            r["branch"],
                            e,
                        )
            except Exception as e:  # noqa: BLE001 — transient; re-poll
                log.warning(
                    "%s: multi-repo merge failed for %s (retry): %s",
                    ticket.id,
                    r["repo_id"],
                    e,
                )
                return Outcome(ticket.state)
        # Next poll sees all PRs merged → DONE.
        return Outcome(ticket.state)
