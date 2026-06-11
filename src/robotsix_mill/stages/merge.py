"""Merge stage: IMPLEMENT_COMPLETE -> HUMAN_MR_APPROVAL (gates passed)
                     -> DONE (merged) | BLOCKED (closed unmerged)
                     -> FIXING_CI (failing CI, deferred)
                     -> REBASING (conflicting, deferred)

HUMAN_MR_APPROVAL -> DONE (merged) | BLOCKED (closed unmerged)
              -> IMPLEMENT_COMPLETE (gate degradation — silent fallback)
              -> WAITING_AUTO_MERGE (eligible, CI pending)

REBASING -> IMPLEMENT_COMPLETE (rebase succeeded, re-verify gates)

FIXING_CI -> IMPLEMENT_COMPLETE (fix succeeded, re-verify gates)

The PR is the review. This stage is re-run by the worker's lightweight
poll while the ticket sits in IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL,
REBASING, FIXING_CI, or WAITING_AUTO_MERGE; it checks the forge:

IMPLEMENT_COMPLETE (gate-check):
- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> check CI status:
    - failing CI    -> FIXING_CI (auto-fix agent)
    - green CI      -> HUMAN_MR_APPROVAL (gates passed! notify human)
    - pending CI    -> IMPLEMENT_COMPLETE (no-op; re-poll)
- open, conflicting -> REBASING (defer rebase agent)

HUMAN_MR_APPROVAL:
- merged            -> DONE
- closed, unmerged  -> BLOCKED (resumable)
- open, mergeable   -> check CI status:
    - failing CI    -> IMPLEMENT_COMPLETE (silent fallback)
    - green CI      -> HUMAN_MR_APPROVAL (no-op; re-poll)
    - pending CI    -> HUMAN_MR_APPROVAL (no-op; re-poll)
- open, conflicting -> IMPLEMENT_COMPLETE (silent fallback)

Returning the *same* state is the worker's "leave it, re-poll" signal —
no history spam, no busy loop.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path

from ..agents.ci_fixing import run_ci_fix_agent
from .ci_fix import _pr_changed_paths
from ..agents.rebasing import run_rebase_agent
from ..agents.review_revision import run_review_revision_agent
from ..config import RepoConfig, get_repo_config
from ..config_loader import ConfigError
from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import _resolve_remote_url, github_token
from ..runners.pass_runner import load_memory, persist_memory
from ..runtime import tracing
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.merge")

_REBASE_COUNTER = "rebase_attempts.txt"
_MERGE_REASON = "merge_reason.txt"
_REV_REV_COUNTER = "review_revision_attempts.txt"


def _load_pr_urls(ws_artifacts_dir: Path) -> list[dict] | None:
    """Read ``pr_urls.json``.

    Returns the list when present + parseable, ``None`` when the file
    is absent (single-repo path), or raises ``ValueError`` on a
    corrupt file so the caller can BLOCK-resumable.

    The schema mirrors what :func:`deliver._write_pr_urls` writes::

        [{"repo_id": str, "branch": str, "url": str}, ...]
    """
    path = ws_artifacts_dir / "pr_urls.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"pr_urls.json could not be parsed: {e}")
    if not isinstance(data, list):
        raise ValueError("pr_urls.json is not a JSON list")
    return data


def _repo_config_for_entry(entry: dict) -> RepoConfig:
    """Resolve a per-repo :class:`RepoConfig` from a ``pr_urls.json``
    entry. Propagates :class:`ConfigError` when the ``repo_id`` is
    missing, non-string, empty, or not registered so the caller's
    existing ``except ConfigError`` arm translates to a BLOCKED
    outcome (instead of bubbling a ``KeyError`` from ``entry['repo_id']``
    when the manifest is malformed)."""
    repo_id = entry.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id:
        raise ConfigError("pr_urls.json entry is missing a non-empty string 'repo_id'")
    return get_repo_config(repo_id)


def _read_counter(path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError, ValueError:
        return 0


def _write_counter(path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def _build_failing_summary(
    failing: list[dict],
    log_text: str = "",
    alerts: list[dict] | None = None,
    changed_paths: set[str] | None = None,
) -> str:
    """Markdown summary of failing checks for the CI-fix agent.

    A thin wrapper over ``stages.ci_fix._build_failing_summary`` (imported
    lazily to avoid a module-load cycle) so the multi-repo path renders the
    same job-logs + code-scanning-alert detail as the single-repo path. When
    *changed_paths* is provided the alerts are partitioned against the PR's
    own diff and labelled in-scope / out-of-scope, mirroring the single-repo
    ``ci_fix._build_failure_detail`` path.
    """
    from .ci_fix import _build_failing_summary as _ci_fix_summary

    return _ci_fix_summary(failing, log_text, alerts, changed_paths)


def _read_reason(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _write_reason(path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reason, encoding="utf-8")


def _workspace_repo_dir(ctx, ticket) -> str | None:
    """Return the ticket's workspace clone dir, or None if missing."""
    ws = ctx.service.workspace(ticket)
    repo = ws.dir / "repo"
    if not (repo / ".git").exists():
        return None
    return str(repo)


def _verify_merge_ancestor(repo_dir: str | None, sha: str, ticket_id: str) -> bool:
    """Verify that commit *sha* is an ancestor of origin/main.

    Fetches origin/main to ensure the local ref is current, then runs
    ``git merge-base --is-ancestor <sha> origin/main``.  When the
    direct ancestry check fails (exit 1), falls back to squash-merge
    detection: greps the origin/main log for *ticket_id*.

    Returns True when the merge is confirmed (ancestor or squash-
    merge found).  Returns False only when the check runs and
    confirms the commit is NOT on origin/main.  When the repo is
    unavailable or a git error occurs, returns True (best-effort —
    do not block the pipeline on transient tooling issues).
    """
    if repo_dir is None or not sha:
        # Nothing to verify — best-effort allow.
        return True
    try:
        subprocess.run(
            ["git", "-C", repo_dir, "fetch", "origin", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        log.warning(
            "%s: git fetch origin main failed — allowing merge (best-effort)",
            ticket_id,
        )
        return True

    result = subprocess.run(
        [
            "git",
            "-C",
            repo_dir,
            "merge-base",
            "--is-ancestor",
            sha,
            "origin/main",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True  # sha is an ancestor of origin/main
    if result.returncode == 1:
        # Not a direct ancestor — maybe it was a squash-merge.
        grep = subprocess.run(
            [
                "git",
                "-C",
                repo_dir,
                "log",
                "origin/main",
                "--oneline",
                "--fixed-strings",
                f"--grep={ticket_id}",
            ],
            capture_output=True,
            text=True,
        )
        if grep.returncode == 0 and grep.stdout.strip():
            log.info(
                "%s: commit %s is not an ancestor of origin/main, "
                "but a commit referencing this ticket was found on "
                "origin/main — treating as squash-merged",
                ticket_id,
                sha[:8],
            )
            return True
        log.info(
            "%s: commit %s is NOT an ancestor of origin/main — merge not confirmed",
            ticket_id,
            sha[:8],
        )
        return False
    # Any other exit code — git error, best-effort allow.
    log.warning(
        "%s: git merge-base --is-ancestor failed for %s — allowing merge (best-effort)",
        ticket_id,
        sha[:8],
    )
    return True


class MergeStage(Stage):
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
        * Any ``conflicting`` -> BLOCKED (per-repo rebase auto-recovery is
          still parked for a follow-up).
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
            elif conclusion == "success":
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
            first = conflicting[0]
            return Outcome(
                State.BLOCKED,
                f"PR for {first['repo_id']} conflicting: {first['url']} — "
                "resumable (multi-repo rebase auto-recovery is not yet wired)",
            )

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

    def _multi_repo_fix_ci(
        self, ticket: Ticket, ctx: StageContext, status: dict
    ) -> Outcome:
        """Run the CI-fix agent on one multi-repo PR whose CI is failing.

        Mirrors the single-repo :class:`CIFixStage` but inline (a multi-repo
        ticket has one state). Bounded by a per-repo attempt counter; a
        ``DONE`` agent run that produces no new commits still counts toward the
        cap so a flaky CI can't loop forever. Exhausting the cap -> BLOCKED.
        Returns the ticket's current state (re-poll) while making progress.
        """
        s = ctx.settings
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

        counter_path = ws.artifacts_dir / f"ci_fix_{repo_id}.count"
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.ci_fix_max_attempts
        if attempt > max_attempts:
            _write_counter(counter_path, 0)
            return Outcome(
                State.BLOCKED,
                f"ci fix for {repo_id} failed after {max_attempts} attempt(s) — "
                "manual intervention required",
            )

        forge = get_forge(s, repo_config=rc)
        try:
            ci = forge.check_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning(
                "%s: check_status failed for %s (retry): %s", ticket.id, repo_id, e
            )
            return Outcome(ticket.state)

        # --- cycle-ceiling gate (mirrors CIFixStage) ---
        conclusion = (ci or {}).get("conclusion")
        cycle_counter_path = ws.artifacts_dir / f"ci_fix_{repo_id}_cycles.txt"
        if conclusion == "success":
            # CI turned green between polls — reset and re-poll.
            _write_counter(cycle_counter_path, 0)
            return Outcome(ticket.state)
        if conclusion == "failure":
            cycles = _read_counter(cycle_counter_path)
            if s.ci_fix_max_cycles > 0 and cycles >= s.ci_fix_max_cycles:
                _write_counter(cycle_counter_path, 0)
                log.warning(
                    "%s: multi-repo ci-fix for %s hit hard ceiling of %d cycle(s) "
                    "without turning CI green — escalating to BLOCKED",
                    ticket.id,
                    repo_id,
                    s.ci_fix_max_cycles,
                )
                return Outcome(
                    State.BLOCKED,
                    f"ci fix for {repo_id} exhausted hard ceiling of "
                    f"{s.ci_fix_max_cycles} cycle(s) without turning CI green "
                    f"— manual intervention required",
                )
            _write_counter(cycle_counter_path, cycles + 1)

        failing = (ci or {}).get("failing", [])

        log_text = ""
        alerts: list[dict] = []
        changed_paths: set[str] = set()
        try:
            alerts = forge.list_code_scanning_alerts(source_branch=branch)
            changed_paths = _pr_changed_paths(forge, branch)
            pr = forge.pr_status(source_branch=branch)
            head_sha = (pr or {}).get("sha", "")
            if head_sha:
                for run in forge.list_workflow_runs(head_sha=head_sha):
                    if run.get("conclusion") == "failure":
                        logs = forge.fetch_workflow_job_logs(run_id=run["id"])
                        if logs:
                            log_text += (
                                f"\n--- {run.get('name', 'workflow')} "
                                f"(run {run['id']}) ---\n{logs}"
                            )
        except Exception:  # noqa: BLE001 — best-effort enrichment
            log.warning(
                "%s: failed to fetch job logs / alerts for %s", ticket.id, repo_id
            )

        failing_summary = _build_failing_summary(
            failing, log_text, alerts, changed_paths
        )
        log.info(
            "%s: multi-repo CI failing for %s — ci-fix attempt %d/%d",
            ticket.id,
            repo_id,
            attempt,
            max_attempts,
        )

        ok = False
        try:
            # Attribute the agent's cost/traces to the ticket's session, and
            # to the TARGET repo's Langfuse project, not an orphan trace.
            with tracing.start_ticket_root_span(ticket.id, "ci_fix", repo_config=rc):
                mem_path = s.memory_file_for("ci_fix", rc.board_id)
                result = run_ci_fix_agent(
                    settings=s,
                    repo_dir=str(repo_dir),
                    branch=branch,
                    failing_summary=failing_summary,
                    memory=load_memory(mem_path),
                    ticket_id=ticket.id,
                    board_id=rc.board_id,
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    persist_memory(mem_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "%s: multi-repo ci-fix crashed for %s: %s", ticket.id, repo_id, e
            )
            ok = False

        if ok:
            # No new commits (agent reported DONE but changed nothing) still
            # counts toward the cap so a flaky check can't loop forever.
            try:
                local = git_ops.head_sha(repo_dir)
                remote = git_ops.remote_branch_sha(repo_dir, branch)
            except Exception:  # noqa: BLE001 — be safe: assume changes
                local, remote = None, "force-push"
            if local is not None and remote == local:
                _write_counter(counter_path, attempt)
                log.info(
                    "%s: multi-repo ci-fix for %s made no changes (attempt %d/%d)",
                    ticket.id,
                    repo_id,
                    attempt,
                    max_attempts,
                )
                return Outcome(ticket.state)
            try:
                git_ops.push(
                    repo_dir,
                    branch=branch,
                    remote_url=_resolve_remote_url(s, rc),
                    token=github_token(s, repo_config=rc),
                )
            except Exception as e:  # noqa: BLE001
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"ci fix for {repo_id} succeeded but force-push failed: {e}",
                )
            _write_counter(counter_path, 0)
            log.info(
                "%s: multi-repo ci fix pushed for %s — re-poll", ticket.id, repo_id
            )
            return Outcome(ticket.state)

        # Agent failed — record the attempt and re-poll.
        _write_counter(counter_path, attempt)
        log.warning(
            "%s: multi-repo ci-fix attempt %d/%d failed for %s — retrying next poll",
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
        ticket eligible. Held (same-state) when not eligible, so a human can
        merge instead. Partial merges are fine — the next poll continues."""
        s = ctx.settings
        eligible, reason = self._auto_merge_eligible(ticket, ctx)
        if not eligible:
            self._maybe_comment(
                ticket, ctx, f"multi-repo PRs green; auto-merge held: {reason}"
            )
            return Outcome(ticket.state)

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

    def _handle_human_mr_approval(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status from HUMAN_MR_APPROVAL: merged/closed/conflicting/CI/auto-merge."""
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.HUMAN_MR_APPROVAL)  # no-op

        if pr is None:
            return Outcome(State.HUMAN_MR_APPROVAL)  # not visible yet — re-poll

        if pr.get("merged"):
            ctx.service.workspace(ticket).artifacts_dir.joinpath("merge.md").write_text(
                f"merged: {pr.get('url', '')}\n", encoding="utf-8"
            )
            self._cleanup_branch_on_done(ticket, ctx, branch)
            log.info("%s: PR merged → done", ticket.id)
            return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )

        # --- Review feedback check (opt-in, gated by config flag) ---
        if s.review_feedback_enabled:
            try:
                review_status = get_forge(
                    s, repo_config=ctx.repo_config
                ).pr_review_status(source_branch=branch)
            except Exception as e:  # noqa: BLE001 — transient
                log.warning("%s: pr_review_status failed (retry): %s", ticket.id, e)
                review_status = None

            if (
                review_status is not None
                and review_status.get("state") == "CHANGES_REQUESTED"
            ):
                # Persist the review comments as an artifact so the agent can
                # read them even if the forge becomes unreachable on the next poll.
                comments = review_status.get("comments", [])
                if comments:
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
                # CHANGES_REQUESTED but no comments — treat as no-op (empty review body).
                log.info(
                    "%s: changes requested with empty body — treating as no-op",
                    ticket.id,
                )

        # PR is open.  Check mergeability.
        mergeable = pr.get("mergeable")
        if mergeable is False:
            # PR is open and conflicting → silent fallback to
            # IMPLEMENT_COMPLETE so the robot can auto-fix (via
            # REBASING) without notifying the human.
            log.info(
                "%s: PR conflicting — falling back to IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                "PR is now conflicting; gates no longer pass",
            )

        # mergeable=True or None (unchecked) → no conflict. This is the
        # only true "rebase made progress" signal — clear the rebase
        # attempt counter so a *later* genuine conflict gets a fresh
        # budget (and so the counter can't accumulate across unrelated
        # conflicts).
        _write_counter(
            ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER,
            0,
        )

        # Check remote CI before returning no-op.
        try:
            ci_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.HUMAN_MR_APPROVAL)

        if ci_status is None:
            # No PR or no data — standard wait.
            return Outcome(State.HUMAN_MR_APPROVAL)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info(
                "%s: mergeable PR has failing CI → falling back to IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE, "CI is failing; gates no longer pass"
            )

        # success, pending, or None — evaluate auto-merge eligibility.
        eligible, eligibility_reason = self._auto_merge_eligible(ticket, ctx)

        if conclusion == "success":
            if eligible:
                # CI green + eligible → auto-merge now.
                result = get_forge(s, repo_config=ctx.repo_config).merge_pr(
                    source_branch=branch
                )
                if result.get("merged"):
                    ctx.service.workspace(ticket).artifacts_dir.joinpath(
                        "merge.md"
                    ).write_text(
                        f"auto-merged: {pr.get('url', '')}\n",
                        encoding="utf-8",
                    )
                    self._cleanup_branch_on_done(ticket, ctx, branch)
                    log.info("%s: auto-merged → done", ticket.id)
                    return Outcome(
                        State.DONE,
                        f"auto-merged: {pr.get('url', '')}",
                    )
                # Forge rejected the merge.
                reason_text = f"forge merge failed: {result.get('reason', 'unknown')}"
                self._maybe_comment(ticket, ctx, reason_text)
                log.warning(
                    "%s: auto-merge failed: %s — falling back to human",
                    ticket.id,
                    result.get("reason", "unknown"),
                )
                return Outcome(State.HUMAN_MR_APPROVAL, reason_text)
            else:
                # CI green but not eligible → human approval needed.
                self._maybe_comment(ticket, ctx, eligibility_reason)
                return Outcome(State.HUMAN_MR_APPROVAL)

        # pending or None — not yet green.
        if eligible:
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        # Not eligible + CI pending → standard human wait.
        self._maybe_comment(ticket, ctx, eligibility_reason)
        return Outcome(State.HUMAN_MR_APPROVAL)

    def _auto_merge_eligible(
        self, ticket: Ticket, ctx: StageContext
    ) -> tuple[bool, str]:
        """Return ``(eligible, reason)`` for auto-merge.

        *eligible* is True when ALL of the following hold:
        1. ``settings.auto_merge_enabled`` is True
        2. ``settings.review_enabled`` is True
        3. Review artifact exists at ``{workspace}/artifacts/review.md``
        4. Artifact contains the literal string ``"auto_merge_eligible: true"``

        *reason* explains the blocking condition when eligible is False.
        """
        s = ctx.settings
        if not s.auto_merge_enabled:
            return False, "auto-merge disabled in config"
        if not s.review_enabled:
            return False, "review gate disabled — human approval required"

        review_artifact = ctx.service.workspace(ticket).artifacts_dir / "review.md"
        if not review_artifact.exists():
            return False, "no review artifact — human approval required"

        review_text = review_artifact.read_text(encoding="utf-8")
        if "auto_merge_eligible: true" not in review_text:
            # Try to read the verdict line for context.
            verdict_note = ""
            comment_note = ""
            for line in review_text.splitlines():
                if line.startswith("verdict:"):
                    verdict_note = " (" + line[len("verdict:") :].strip()[:200] + ")"
                elif line.startswith("comment:"):
                    raw = line[len("comment:") :].strip()
                    if raw and raw != "(no details)":
                        comment_note = " — " + raw[:300]
            return (
                False,
                "reviewer marked not auto-merge eligible" + verdict_note + comment_note,
            )

        return True, "eligible"

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

    def _poll_waiting_auto_merge(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Re-poll CI for a ticket in WAITING_AUTO_MERGE.

        The ticket was already determined eligible for auto-merge; CI was
        pending. On each poll:
        - CI success → try auto-merge (DONE or HUMAN_MR_APPROVAL on forge reject)
        - CI failure → FIXING_CI
        - CI still pending → WAITING_AUTO_MERGE (same-state no-op)
        - Eligibility lost → HUMAN_MR_APPROVAL with comment
        """
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        # First, re-check eligibility (review artifact may have changed).
        eligible, reason = self._auto_merge_eligible(ticket, ctx)
        if not eligible:
            self._maybe_comment(ticket, ctx, reason)
            return Outcome(State.HUMAN_MR_APPROVAL, reason)

        # Re-check PR status (could have become conflicting).
        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.WAITING_AUTO_MERGE)

        if pr is None:
            return Outcome(State.WAITING_AUTO_MERGE)
        if pr.get("merged"):
            sha = pr.get("sha", "")
            repo_dir = _workspace_repo_dir(ctx, ticket)
            if _verify_merge_ancestor(repo_dir, sha, ticket.id):
                ctx.service.workspace(ticket).artifacts_dir.joinpath(
                    "merge.md"
                ).write_text(f"merged: {pr.get('url', '')}\n", encoding="utf-8")
                self._cleanup_branch_on_done(ticket, ctx, branch)
                log.info("%s: PR merged → done", ticket.id)
                return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
            log.warning(
                "%s: PR reported merged but commit %s is not an ancestor of "
                "origin/main — falling back to IMPLEMENT_COMPLETE for investigation",
                ticket.id,
                sha[:8] if sha else "(none)",
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                f"PR reported merged but merge not confirmed on origin/main: {pr.get('url', '')}",
            )
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )
        mergeable = pr.get("mergeable")
        if mergeable is False:
            log.info(
                "%s: PR became conflicting while waiting for CI → IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE, "PR is now conflicting; gates no longer pass"
            )

        # Check CI.
        try:
            ci_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.WAITING_AUTO_MERGE)

        if ci_status is None:
            # No CI data yet — keep waiting.
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info(
                "%s: CI failed while waiting for auto-merge → IMPLEMENT_COMPLETE",
                ticket.id,
            )
            self._maybe_comment(ticket, ctx, "CI failed — falling back to gate check")
            return Outcome(State.IMPLEMENT_COMPLETE, "CI failed; gates no longer pass")

        if conclusion == "success":
            # CI is green — attempt auto-merge.
            feature_tip_sha = pr.get("sha", "")  # capture before merge
            result = get_forge(s, repo_config=ctx.repo_config).merge_pr(
                source_branch=branch
            )
            if result.get("merged"):
                repo_dir = _workspace_repo_dir(ctx, ticket)
                if _verify_merge_ancestor(repo_dir, feature_tip_sha, ticket.id):
                    ctx.service.workspace(ticket).artifacts_dir.joinpath(
                        "merge.md"
                    ).write_text(
                        f"auto-merged: {pr.get('url', '')}\n",
                        encoding="utf-8",
                    )
                    self._cleanup_branch_on_done(ticket, ctx, branch)
                    log.info("%s: auto-merged → done", ticket.id)
                    return Outcome(
                        State.DONE,
                        f"auto-merged: {pr.get('url', '')}",
                    )
                log.warning(
                    "%s: auto-merge reported success but commit %s is not an "
                    "ancestor of origin/main — falling back to IMPLEMENT_COMPLETE",
                    ticket.id,
                    feature_tip_sha[:8] if feature_tip_sha else "(none)",
                )
                return Outcome(
                    State.IMPLEMENT_COMPLETE,
                    f"auto-merge reported success but merge not confirmed on origin/main: {pr.get('url', '')}",
                )
            # Forge rejected the merge.
            reason_text = f"forge merge failed: {result.get('reason', 'unknown')}"
            self._maybe_comment(ticket, ctx, reason_text)
            log.warning(
                "%s: auto-merge failed: %s — falling back to human",
                ticket.id,
                result.get("reason", "unknown"),
            )
            return Outcome(State.HUMAN_MR_APPROVAL, reason_text)

        # Pending or None — keep waiting.
        self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
        return Outcome(State.WAITING_AUTO_MERGE)

    def _poll_implement_complete(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status for a ticket in IMPLEMENT_COMPLETE.

        Verify two gates before promoting to HUMAN_MR_APPROVAL:
        1. CI is green.
        2. PR is mergeable (no conflict with target).

        - Both gates pass → HUMAN_MR_APPROVAL (notify human).
        - CI failing → FIXING_CI (defer CI-fix agent).
        - Conflicting → REBASING (defer rebase agent).
        - CI pending / no data → same-state IMPLEMENT_COMPLETE (re-poll).
        - PR merged while polling → DONE.
        - PR closed → BLOCKED.
        """
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if pr is None:
            return Outcome(State.IMPLEMENT_COMPLETE)  # not visible yet — re-poll
        if pr.get("merged"):
            ctx.service.workspace(ticket).artifacts_dir.joinpath("merge.md").write_text(
                f"merged: {pr.get('url', '')}\n", encoding="utf-8"
            )
            self._cleanup_branch_on_done(ticket, ctx, branch)
            log.info("%s: PR merged → done", ticket.id)
            return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )

        # PR is open.  Check mergeability.
        mergeable = pr.get("mergeable")
        if mergeable is False:
            log.info(
                "%s: PR conflicting in IMPLEMENT_COMPLETE → REBASING",
                ticket.id,
            )
            return Outcome(
                State.REBASING,
                "PR is conflicting; rebase agent will run next poll",
            )

        # mergeable=True or None (unchecked) → no conflict.
        # Clear rebase attempt counter — this is signal of progress.
        _write_counter(
            ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER,
            0,
        )

        # Check remote CI.
        try:
            ci_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if ci_status is None:
            # No CI data yet — keep waiting.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            # Rebase BEFORE ci_fix when the branch is behind main. A repo-wide
            # gate (ruff/mypy/lint over the whole tree) often fails on code that
            # isn't this ticket's diff — the branch was cut from an older main
            # and main has since gained the fix. ci_fix can't repair non-ticket
            # code, but a rebase onto current main can. Self-gating: after one
            # rebase the branch is no longer behind, so a still-failing CI then
            # routes to ci_fix (a genuine, ticket-owned failure). Skipped when
            # the workspace clone is gone (None) — fall straight to ci_fix.
            repo_dir = _workspace_repo_dir(ctx, ticket)
            if repo_dir is not None and git_ops.branch_is_behind_main(Path(repo_dir)):
                log.info(
                    "%s: CI failing on a stale base (branch behind main) → "
                    "REBASING before ci_fix",
                    ticket.id,
                )
                return Outcome(
                    State.REBASING,
                    "CI failing and branch is behind main; rebasing onto current "
                    "main before ci_fix (the failure may be pre-existing repo-wide "
                    "debt the branch lacks the fix for)",
                )
            log.info("%s: CI failing → FIXING_CI", ticket.id)
            return Outcome(State.FIXING_CI)

        if conclusion == "success":
            # Both gates passed! Promote to human review. This is the only
            # GENUINE "CI is fixed" signal (sustained green that advances the
            # ticket), so reset the ci_fix hard cycle ceiling here — not on a
            # transient green read inside ci_fix (which a flickering CI emits
            # between failing cycles and which let a runaway loop survive).
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / "ci_fix_cycles.txt",
                0,
            )
            log.info("%s: gates passed → HUMAN_MR_APPROVAL", ticket.id)
            return Outcome(
                State.HUMAN_MR_APPROVAL,
                "CI checks green and PR is mergeable — awaiting human merge approval",
            )

        # pending or None — keep waiting.
        return Outcome(State.IMPLEMENT_COMPLETE)

    def _run_rebase(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Execute the rebase agent for a ticket already in REBASING."""
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        return self._handle_conflict(ticket, ctx, branch)

    def _run_review_revision(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Execute the review-revision agent for a ticket in ADDRESSING_REVIEW."""
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        repo_dir = _workspace_repo_dir(ctx, ticket)
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
            with tracing.start_ticket_root_span(ticket.id, "review_revision"):
                review_revision_memory_path = s.memory_file_for(
                    "review_revision", ctx.memory_board_id(ticket)
                )
                memory_text = load_memory(review_revision_memory_path)
                result = run_review_revision_agent(
                    settings=s,
                    repo_dir=Path(repo_dir),
                    branch=branch,
                    review_comments=review_comments_text,
                    pr_files=pr_files,
                    memory=memory_text,
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    persist_memory(review_revision_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: review-revision agent crashed: %s", ticket.id, e)
            ok = False

        if ok:
            # Only force-push when the local HEAD differs from remote.
            try:
                local = git_ops.head_sha(repo_dir)
                remote = git_ops.remote_branch_sha(repo_dir, branch)
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
                git_ops.push(
                    repo_dir,
                    branch=branch,
                    remote_url=_resolve_remote_url(s, ctx.repo_config),
                    token=github_token(s, repo_config=ctx.repo_config),
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

    def _handle_conflict(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> Outcome:
        """Attempt rebase for a conflicting PR."""
        s = ctx.settings

        repo_dir = self._validate_workspace_for_rebase(ctx, ticket)
        if isinstance(repo_dir, Outcome):
            return repo_dir

        counter_path, attempt, max_attempts = self._read_rebase_attempt(ctx, ticket, s)

        target = s.forge_target_branch
        log.info(
            "%s: PR conflicting — rebase attempt %d/%d onto %s",
            ticket.id,
            attempt,
            max_attempts,
            target,
        )

        ok = self._fetch_and_run_rebase(
            ticket, s, ctx.repo_config, repo_dir, branch, target, attempt
        )

        if ok:
            return self._handle_rebase_success(
                ticket, ctx, branch, repo_dir, counter_path, attempt, max_attempts
            )
        return self._handle_rebase_failure(ticket, counter_path, attempt, max_attempts)

    def _validate_workspace_for_rebase(
        self, ctx: StageContext, ticket: Ticket
    ) -> str | Outcome:
        """Return the repo_dir string, or an Outcome to return early if missing."""
        repo_dir = _workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return Outcome(
                State.BLOCKED,
                "PR is conflicting but workspace clone is missing; "
                "cannot rebase. Re-run implement to recreate the clone.",
            )
        return repo_dir

    def _read_rebase_attempt(
        self, ctx: StageContext, ticket: Ticket, s
    ) -> tuple[Path, int, int]:
        """Return (counter_path, attempt, max_attempts) for the current rebase."""
        counter_path = ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.rebase_max_attempts
        return counter_path, attempt, max_attempts

    def _fetch_and_run_rebase(
        self,
        ticket: Ticket,
        s,
        repo_config,
        repo_dir: str,
        branch: str,
        target: str,
        attempt: int,
    ) -> bool:
        """Fetch target branch and invoke the rebase agent. Returns True on success."""
        try:
            # The merge stage is traced=False (poll-driven, normally no
            # LLM), so the worker does NOT open the ticket's root span.
            # The rebase agent IS an LLM run — wrap it in the ticket's
            # Langfuse session (session.id = ticket.id) so its cost and
            # traces are attributed to the ticket, not an orphan root
            # trace. (This is what made the overnight rebase cost
            # invisible in the per-ticket session total.)
            # Build the context-manager stack based on attempt number.
            # On the first attempt: open a ticket-root span so
            # Langfuse attributes the rebase agent's LLM cost/traces
            # to the ticket's session.  Retries (attempt > 1) skip
            # the root span to avoid creating duplicate Langfuse
            # traces for the same logical rebase operation.
            stack = contextlib.ExitStack()
            if attempt == 1:
                stack.enter_context(tracing.start_ticket_root_span(ticket.id, "rebase"))
            stack.enter_context(tracing.trace_stage("rebase"))
            with stack:
                # Refresh origin/<target> so the agent rebases onto
                # current main, not the stale ref frozen at clone time.
                # The sandbox has --network none; git fetch MUST run
                # here, outside the container.
                #
                # Use the per-repo remote_url + a freshly-minted
                # token — the global ``forge_remote_url`` and a
                # tokenless mint would both point at the wrong repo
                # (or carry an expired token) for any ticket whose
                # repo isn't the mill's own.
                git_ops.fetch(
                    Path(repo_dir),
                    remote_url=_resolve_remote_url(s, repo_config),
                    token=github_token(s, repo_config=repo_config),
                    branch=target,
                )
                rebase_memory_path = s.memory_file_for(
                    "rebase",
                    (repo_config.board_id if repo_config else "")
                    or s.board_id
                    or ticket.board_id,
                )
                memory_text = load_memory(rebase_memory_path)
                result = run_rebase_agent(
                    settings=s,
                    repo_dir=repo_dir,
                    branch=branch,
                    target=target,
                    memory=memory_text,
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    persist_memory(rebase_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: rebase attempt failed: %s", ticket.id, e)
            ok = False
        return ok

    def _handle_rebase_success(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        repo_dir: str,
        counter_path: Path,
        attempt: int,
        max_attempts: int,
    ) -> Outcome:
        """Handle a successful rebase: SHA guard, force-push, outcome routing."""
        s = ctx.settings
        # Only force-push when the remote doesn't already have this
        # exact commit. GitHub reports mergeable=False transiently
        # right after any push (while it recomputes); pushing an
        # unchanged branch re-triggers CI + another recompute →
        # endless REBASING↔HUMAN_MR_APPROVAL ping-pong on a healthy PR (and
        # an ntfy every cycle). The merge stage fetched
        # origin/<branch> before invoking the agent, so
        # origin/<branch> is fresh.
        try:
            local = git_ops.head_sha(repo_dir)
            remote = git_ops.remote_branch_sha(repo_dir, branch)
        except Exception:  # noqa: BLE001 — be safe: fall back to push
            local, remote = None, "force-push"

        if local is not None and remote == local:
            # Nothing to push. The rebase made no change yet GitHub
            # still flags the PR — either GitHub is still recomputing
            # (it will clear on a later poll → merge) or the local
            # base is stale / the conflict is genuinely unresolvable.
            # This is NOT progress: count it (don't reset) and bound
            # the loop. Stay REBASING — a same-state no-op the worker
            # leaves alone (no transition, no ntfy) — until the
            # attempt budget is spent, then BLOCKED once.
            if attempt < max_attempts:
                _write_counter(counter_path, attempt)
                log.info(
                    "%s: rebase no-op (remote already current) — "
                    "GitHub still flags conflict; re-poll %d/%d",
                    ticket.id,
                    attempt,
                    max_attempts,
                )
                return Outcome(State.REBASING)  # silent re-poll
            _write_counter(counter_path, 0)
            log.warning(
                "%s: rebase keeps being a no-op but the PR is still "
                "conflicting after %d attempts",
                ticket.id,
                max_attempts,
            )
            return Outcome(
                State.BLOCKED,
                "rebase is a no-op yet GitHub still reports the PR "
                "conflicting — the local clone's base is likely stale "
                "or the conflict needs manual resolution. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        # Remote is behind / missing → genuine push needed.
        # Push to the *per-repo* remote with a per-repo token — the global
        # ``s.forge_remote_url`` + tokenless mint point at the mill's own
        # repo, so for any ticket on another board the rebased commit
        # lands on the wrong remote, the real PR branch never changes, and
        # the loop blocks ("force-pushed Nx but still conflicting"). Mirror
        # the fetch above, which already resolves these per-repo.
        try:
            git_ops.push(
                repo_dir,
                branch=branch,
                remote_url=_resolve_remote_url(s, ctx.repo_config),
                token=github_token(s, repo_config=ctx.repo_config),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("%s: force-push after rebase failed: %s", ticket.id, e)
            _write_counter(counter_path, attempt)
            return Outcome(
                State.BLOCKED,
                f"rebase succeeded but force-push failed: {e}",
            )
        # Pushed — but a push is NOT proof the conflict is resolved
        # (git rebase rewrites SHAs every run, so "pushed" happens
        # even when the rebase keeps failing to truly resolve and
        # GitHub still reports the PR conflicting). Only an actually
        # mergeable PR clears the counter (in the HUMAN_MR_APPROVAL path).
        # So persist the attempt and bound the loop here too.
        log.info("%s: rebase succeeded, branch force-pushed", ticket.id)
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            # Route by context: no PR yet → back to implement; PR exists → re-check gates.
            try:
                pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                    source_branch=branch
                )
            except Exception:
                pr = None
            next_state = State.READY if pr is None else State.IMPLEMENT_COMPLETE
            return Outcome(next_state)
        _write_counter(counter_path, 0)  # reset for a future resume
        return Outcome(
            State.BLOCKED,
            f"rebased and force-pushed {max_attempts}x but GitHub "
            "still reports the PR conflicting — the local clone's "
            "base is likely stale or the conflict is unresolvable "
            "automatically. Resume-blocked to retry from human_mr_approval.",
        )

    def _handle_rebase_failure(
        self,
        ticket: Ticket,
        counter_path: Path,
        attempt: int,
        max_attempts: int,
    ) -> Outcome:
        """Handle a failed rebase: retry counting or BLOCKED when exhausted."""
        if attempt < max_attempts:
            _write_counter(counter_path, attempt)
            log.warning(
                "%s: rebase attempt %d/%d failed — retrying next poll",
                ticket.id,
                attempt,
                max_attempts,
            )
            return Outcome(State.REBASING)  # no-op; retry next poll

        # Exhausted all attempts.
        _write_counter(counter_path, 0)  # reset for any future resume
        return Outcome(
            State.BLOCKED,
            f"rebase failed after {max_attempts} attempt(s) — "
            "manual conflict resolution required. "
            "Resume-blocked to retry from human_mr_approval.",
        )
