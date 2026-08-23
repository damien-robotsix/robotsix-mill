"""Review stage: CODE_REVIEW -> DOCUMENTING | READY | AWAITING_USER_REPLY.

Runs a blind dual-model review of the implementation diff. The review
agent sees ONLY the git diff and ticket spec — no implementation
context.  APPROVE → DOCUMENTING; REQUEST_CHANGES → READY (with review
comments stored); NEEDS_DISCUSSION → AWAITING_USER_REPLY (posts the
verdict as an [ASK_USER] thread; operator's reply auto-resumes review).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..agents.reviewing import (
    ReviewAsk,
    ReviewVerdict,
    changed_line_ranges_from_diff,
    run_review_agent,
)
from ..config import target_branch_for
from ..config.repos import get_repos_config
from ..config.settings import Settings
from ..core.models import Ticket
from ..core.states import State
from ..core.workspace import Workspace
from ..forge.auth import github_token
from ..forge.github import _parse_owner_repo
from ..vcs import git_ops
from ._implemented_repos import ImplementedRepo, combined_diff, implemented_repos
from ._review_helpers import (
    _SHA_RE,
    _action_refs_from_diff,
    _build_prior_context,
    _collapse_comments,
    _detect_convergence,
    _file_in_scope,  # noqa: F401 — re-export for test imports
    _gaps_already_addressed,
    _load_file_map,
    _maybe_cache,
    _reusable_workflow_sha_refs_from_diff,
    _sanitize_comments,
    _spawn_dependency_tickets,
    _split_asks,
    _validate_action_refs,
    _verify_action_sha,
    _verify_already_addressed_asks,
    _workflow_refs_from_diff,
)
from .base import Outcome, Stage, StageContext
from .implement._shared import (
    _is_config_only_change,
    _is_rename_only_change,
    _is_small_mechanical_refactor,
)

log = logging.getLogger("robotsix_mill.stages.review")


class _DiffMeta:
    """Resolved diff and metadata bundle returned by
    :meth:`ReviewStage._resolve_diff_and_metadata`.

    Carries the bounded diff, HEAD SHA, input hash, repo directory,
    extracted paths/refs, and an optional GitHub token — everything
    the orchestrator needs downstream without threading a dozen local
    variables.
    """

    __slots__ = (
        "action_refs",
        "changed_line_ranges",
        "diff",
        "gh_token",
        "head_sha",
        "input_hash",
        "modified_paths",
        "repo_dir",
        "reusable_workflow_refs",
        "workflow_refs",
    )

    def __init__(
        self,
        diff: str,
        head_sha: str,
        input_hash: str,
        repo_dir: Path,
        modified_paths: list[str],
        changed_line_ranges: dict[str, list[tuple[int, int]]],
        workflow_refs: set[str],
        action_refs: list[tuple[str, str, str, str]],
        reusable_workflow_refs: list[tuple[str, str, str, str]],
        gh_token: str | None,
    ) -> None:
        self.diff = diff
        self.head_sha = head_sha
        self.input_hash = input_hash
        self.repo_dir = repo_dir
        self.modified_paths = modified_paths
        self.changed_line_ranges = changed_line_ranges
        self.workflow_refs = workflow_refs
        self.action_refs = action_refs
        self.reusable_workflow_refs = reusable_workflow_refs
        self.gh_token = gh_token


class ReviewStage(Stage):
    """Check out the target branch and perform automated code review on the ticket's implemented changes."""

    name = "review"
    input_state = State.CODE_REVIEW
    traced = True

    # ── orchestrator ────────────────────────────────────────────────
    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a CODE_REVIEW ticket: refresh the clone, check out the
        ticket branch, and run the automated reviewer agent against the
        diff.
        """
        s = ctx.settings
        ws = ctx.service.workspace(ticket)

        repos = implemented_repos(ws, s, ticket)
        if not repos:
            return Outcome(
                State.BLOCKED,
                "no repository clone to review (re-run implement)",
            )

        target_branch = target_branch_for(s, ctx.repo_config)

        # 1. Resolve diff + metadata; short-circuit on empty diff / cache hit.
        dm = self._resolve_diff_and_metadata(ws, s, ctx, repos, target_branch, ticket)
        if isinstance(dm, Outcome):
            return dm

        spec = ws.read_description()
        prior_context = _build_prior_context(ticket, ctx, ws)

        board_png = ws.artifacts_dir / "board.png"
        screenshot_path = board_png if board_png.exists() else None

        # 2. Cross-repo reusable-workflow clones.
        extra_roots = self._clone_cross_repo_workflows(
            ws, s, ctx, dm.workflow_refs, ticket
        )

        # 3. Model-level routing for cheap changes.
        level = self._resolve_review_level(dm.repo_dir, target_branch)

        # 4. Run the blind review agent.
        try:
            verdict: ReviewVerdict = run_review_agent(
                settings=s,
                diff=dm.diff,
                spec=spec,
                level=level,
                prior_context=prior_context,
                repo_dir=dm.repo_dir,
                reference_files=dm.modified_paths,
                changed_line_ranges=dm.changed_line_ranges,
                screenshot_path=screenshot_path,
                extra_roots=extra_roots,
            )
        except Exception as e:
            log.exception("%s: review agent error", ticket.id)
            # Transient model blips (OpenRouter 5xx/429/timeout, the
            # DeepSeek reasoning-400) should get a fresh stage re-run via
            # the worker's stage-retry rather than a hard BLOCK needing a
            # manual resume — same fix as implement.py.
            from ..runtime.transient_errors import (
                is_insufficient_credit,
                parse_credit_shortfall,
                reraise_if_transient,
            )

            if is_insufficient_credit(e):
                from ..runtime.credit_status import record_low_credit

                detail = parse_credit_shortfall(e)
                record_low_credit(detail=detail)

            reraise_if_transient(e)
            return Outcome(
                State.BLOCKED,
                f"review agent error — resumable: {e}",
            )

        # 5. Action-ref SHA validation (mutates verdict on violations).
        verdict = self._validate_action_shas(
            dm.action_refs, dm.reusable_workflow_refs, dm.gh_token, verdict
        )

        # 6. Persist review artifact.
        ws.artifacts_dir.joinpath("review.md").write_text(
            f"verdict: {verdict.verdict}\n"
            f"auto_merge_eligible: {str(verdict.auto_merge_eligible).lower()}\n"
            f"head_sha: {dm.head_sha}\n"
            f"board_screenshot: {'present' if screenshot_path else 'absent'}\n"
            f"comment: {_collapse_comments(verdict.comments)}\n",
            encoding="utf-8",
        )

        # 7. Route verdict to the next stage.
        return self._handle_review_verdict(
            verdict,
            ticket,
            ctx,
            ws,
            s,
            dm.input_hash,
            dm.modified_paths,
            dm.repo_dir,
        )

    # ── private helpers ─────────────────────────────────────────────

    def _resolve_diff_and_metadata(
        self,
        ws: Workspace,
        s: Settings,
        ctx: StageContext,
        repos: list[ImplementedRepo],
        target_branch: str | None,
        ticket: Ticket,
    ) -> _DiffMeta | Outcome:
        """Compute the combined diff, extract metadata, and handle early
        returns (empty diff, stage-outcome cache hit).

        Returns a :class:`_DiffMeta` bundle on success, or an
        :class:`Outcome` that *run* should return immediately.
        """
        # Compute the combined diff across every implemented clone. Each
        # repo is fetched with a freshly-minted token for ITS forge (the
        # baked-in clone token expires ~1h after clone, so a stale origin
        # URL would 401 on the fetch). For >1 repo, prefix each repo's
        # diff with a header so the reviewer can tell them apart.
        try:
            diff = combined_diff(s, ctx.repo_config, repos, target_branch or "")
        except Exception as e:
            from ..runtime.transient_errors import reraise_if_transient
            from ..vcs.git_ops import redact_credentials

            reraise_if_transient(e)
            # str(CalledProcessError) reprs the full argv — including
            # the tokenized fetch URL. Redact before it hits the note.
            return Outcome(
                State.BLOCKED,
                f"failed to compute diff: {redact_credentials(str(e))}",
            )

        # The review agent's file tools are rooted at the first clone;
        # for multi-repo the per-file pre-seed (below) carries the rest.
        repo_dir = repos[0].repo_dir

        # Empty diff → no-op implementation, approve so deliver can handle it.
        if not diff.strip():
            log.info("%s: empty diff — approving without review", ticket.id)
            return Outcome(State.DOCUMENTING, "empty diff (no-op implementation)")

        # Snapshot the branch-tip HEAD SHA so downstream consumers
        # (stage cache, auto-merge eligibility) can detect when a later
        # rebase or force-push has made this review stale.
        head_sha = git_ops.head_sha(Path(repo_dir))

        # --- stage-outcome cache: short-circuit when input is unchanged ---
        from ._stage_cache import _check, review_input_hash

        input_hash = review_input_hash(
            ws, diff, head_sha, Path(repo_dir), review_rounds=ticket.review_rounds
        )
        cached = _check(ws, ReviewStage.name, input_hash)
        if cached is not None:
            log.info(
                "%s: review cache hit (hash=%s…) → %s",
                ticket.id,
                input_hash[:12],
                cached.next_state.value,
            )
            return cached

        # Derive modified paths, per-file changed line ranges, workflow
        # refs, AND action refs from the UNTRUNCATED diff so middle
        # truncation (below) never drops a ``+++ b/<path>`` header, a
        # hunk, or a ``uses:`` line and silently shrinks the preseed
        # file/excerpt set, the cross-repo clone set, or the action-ref
        # validation. The agent receives the bounded diff; the preseed
        # and extra_roots still cover every referenced file and repo.
        modified_paths = git_ops._paths_from_diff(diff)
        changed_line_ranges = changed_line_ranges_from_diff(diff)
        workflow_refs = _workflow_refs_from_diff(diff)
        action_refs = _action_refs_from_diff(diff)
        reusable_workflow_refs = _reusable_workflow_sha_refs_from_diff(diff)

        # Fetch a GitHub token for authenticated SHA verification against
        # private repos.  When no token is configured (e.g. test
        # environments), ``gh_token`` stays ``None`` and
        # ``_verify_action_sha`` uses the public URL — which degrades
        # gracefully (returns ``None`` = could not check).
        try:
            gh_token = github_token(s, ctx.repo_config)
        except Exception:
            gh_token = None

        # Bound the combined diff before it reaches the review prompt. The
        # raw ``git diff origin/<target>...HEAD`` can balloon to megabytes
        # (divergent base, generated/lockfile churn, accumulated branch
        # history) regardless of how few lines the intended change touches,
        # overflowing even a 1M-token model context. Middle-truncate so both
        # early and late files keep representation. 0 disables the cap.
        from ..core.text_utils import head_tail_keep, limit_diff_context

        if s.review_diff_max_chars > 0 and len(diff) > s.review_diff_max_chars:
            # Over threshold: thin each hunk's context runs first (cheap,
            # preserves every change line) and only then apply the hard
            # head+tail cap so early and late files stay represented.
            diff = limit_diff_context(diff, s.review_diff_context_lines)
            diff = head_tail_keep(diff, s.review_diff_max_chars, label="git-diff")

        return _DiffMeta(
            diff=diff,
            head_sha=head_sha,
            input_hash=input_hash,
            repo_dir=repo_dir,
            modified_paths=modified_paths,
            changed_line_ranges=changed_line_ranges,
            workflow_refs=workflow_refs,
            action_refs=action_refs,
            reusable_workflow_refs=reusable_workflow_refs,
            gh_token=gh_token,
        )

    def _clone_cross_repo_workflows(
        self,
        ws: Workspace,
        s: Settings,
        ctx: StageContext,
        workflow_refs: set[str],
        ticket: Ticket,
    ) -> list[Path] | None:
        """Clone sibling repos referenced by reusable-workflow ``uses:``
        lines so the review agent can inspect their interface.

        Returns a list of :class:`~pathlib.Path` entries (``extra_roots``)
        or ``None`` when no cross-repo clones were needed.
        """
        if not workflow_refs:
            return None

        # Exclude the current repo — the agent already has repo_dir.
        current_remote = (
            ctx.repo_config.forge_remote_url if ctx.repo_config else None
        ) or s.forge_remote_url
        if current_remote:
            try:
                current_owner, current_repo = _parse_owner_repo(current_remote)
                current_slug = f"{current_owner}/{current_repo}"
                workflow_refs.discard(current_slug)
            except Exception:
                log.debug(
                    "%s: cannot parse current repo remote, skipping exclusion",
                    ticket.id,
                )

        if not workflow_refs:
            return None

        clone_roots: list[Path] = []

        # Resolve refs via repos config: for each referenced
        # owner/repo, pick up an existing clone (meta-layout
        # or prior .review-roots clone) or clone fresh.
        try:
            all_repos = get_repos_config().repos
        except Exception:
            all_repos = {}

        for repo_id, rc in all_repos.items():
            remote = rc.forge_remote_url
            if not remote:
                continue
            try:
                owner, repo = _parse_owner_repo(remote)
            except Exception:
                log.debug(
                    "%s: cannot parse remote %s, skipping",
                    ticket.id,
                    remote,
                )
                continue
            slug = f"{owner}/{repo}"
            if slug not in workflow_refs:
                continue

            # 1) Already cloned in .review-roots (prior pass)
            dest = ws.dir / ".review-roots" / repo_id
            if dest.is_dir():
                clone_roots.append(dest)
                continue

            # 2) Already cloned in meta-layout
            meta_dest = ws.dir / "repos" / repo_id
            if meta_dest.is_dir():
                clone_roots.append(meta_dest)
                continue

            # 3) Clone fresh, respecting per-repo branch override
            try:
                token = github_token(s, repo_config=rc)
                branch = target_branch_for(s, rc)
                git_ops.clone(remote, dest, branch, token)
                clone_roots.append(dest)
            except Exception:
                log.warning(
                    "%s: failed to clone %s for cross-repo review",
                    ticket.id,
                    slug,
                )

        return clone_roots or None

    @staticmethod
    def _resolve_review_level(
        repo_dir: Path,
        target_branch: str | None,
    ) -> int | None:
        """Return 1 for cheap (config-only / rename-only / small-refactor)
        changes so the review runs on the level-1 model; ``None`` (use
        default level) otherwise.  Fail-closed: any git error returns
        ``None``.
        """
        if target_branch is None:
            return None
        config_only: bool = _is_config_only_change(repo_dir, target_branch)
        rename_only: bool = _is_rename_only_change(repo_dir, target_branch)
        small_refactor: bool = _is_small_mechanical_refactor(repo_dir, target_branch)
        if config_only or rename_only or small_refactor:
            return 1
        return None

    @staticmethod
    def _validate_action_shas(
        action_refs: list[tuple[str, str, str, str]],
        reusable_workflow_refs: list[tuple[str, str, str, str]],
        gh_token: str | None,
        verdict: ReviewVerdict,
    ) -> ReviewVerdict:
        """Validate 40-char hex SHA refs in *action_refs* and
        *reusable_workflow_refs* via ``git ls-remote``, injecting any
        missing-SHA violations as synthetic REQUEST_CHANGES.

        Returns *verdict* (mutated in-place when violations are found).
        """
        action_violations = _validate_action_refs(action_refs)

        # Optional best-effort existence check for SHA refs:
        # for each ref that IS a 40-char hex SHA, confirm it exists via
        # ``git ls-remote``.  Any failure (network error, timeout,
        # non-zero exit) degrades gracefully — the SHA is not flagged.
        # Tag references (e.g. @v4) are skipped here.
        for file_path, slug, ref, comment in action_refs:
            if _SHA_RE.match(ref):
                parts = slug.split("/")
                if len(parts) >= 2:
                    owner_repo = f"{parts[0]}/{parts[1]}"
                    exists = _verify_action_sha(owner_repo, ref, gh_token)
                    if exists is False:
                        action_violations.append(
                            {
                                "file": file_path,
                                "slug": slug,
                                "ref": ref,
                                "comment": comment,
                            }
                        )

        # Validate SHA refs from reusable-workflow ``uses:`` lines
        # (previously skipped by ``_action_refs_from_diff``).
        for file_path, slug, ref, comment in reusable_workflow_refs:
            parts = slug.split("/")
            if len(parts) >= 2:
                owner_repo = f"{parts[0]}/{parts[1]}"
                exists = _verify_action_sha(owner_repo, ref, gh_token)
                if exists is False:
                    action_violations.append(
                        {
                            "file": file_path,
                            "slug": slug,
                            "ref": ref,
                            "comment": comment,
                        }
                    )

        if action_violations:
            synthetic_asks: list[ReviewAsk] = []
            for v in action_violations:
                comment_part = f" # {v['comment']}" if v["comment"] else ""
                title = (f"Verify commit SHA for {v['slug']} in {v['file']}")[:80]
                description = (
                    f"Commit SHA `{v['ref']}` for action "
                    f"`{v['slug']}{comment_part}` in `{v['file']}` "
                    f"was not found in the upstream repository. "
                    f"Verify the SHA is correct or replace it with a "
                    f"valid 40-char commit SHA."
                )
                synthetic_asks.append(
                    ReviewAsk(
                        title=title,
                        description=description,
                        files_touched=[v["file"]],
                    )
                )

            # Force REQUEST_CHANGES regardless of LLM verdict.
            # Non-existent commit SHAs are a hard correctness issue.
            verdict.verdict = "REQUEST_CHANGES"
            verdict.auto_merge_eligible = False
            verdict.request_changes = synthetic_asks + list(verdict.request_changes)
            if verdict.comments:
                verdict.comments = (
                    "Action ref validation failed: commit SHA not found "
                    "in upstream repo (see request_changes entries "
                    "below).\n\n" + verdict.comments
                )
            else:
                verdict.comments = (
                    "Action ref validation failed: commit SHA not found "
                    "in upstream repo (see request_changes entries "
                    "below)."
                )

        return verdict

    def _handle_review_verdict(
        self,
        verdict: ReviewVerdict,
        ticket: Ticket,
        ctx: StageContext,
        ws: Workspace,
        s: Settings,
        input_hash: str,
        modified_paths: list[str],
        repo_dir: Path,
    ) -> Outcome:
        """Route *verdict* to the next pipeline state.

        APPROVE → DOCUMENTING; REQUEST_CHANGES → READY (or BLOCKED on
        convergence / round-cap exhaustion); NEEDS_DISCUSSION →
        AWAITING_USER_REPLY.
        """
        if verdict.verdict == "APPROVE":
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(State.DOCUMENTING, "review approved")
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        if verdict.verdict == "REQUEST_CHANGES":
            return self._handle_request_changes(
                verdict, ticket, ctx, ws, s, input_hash, modified_paths, repo_dir
            )

        # NEEDS_DISCUSSION — genuine human-decision verdict.
        # Post as [ASK_USER] and pause; operator reply auto-resumes.
        ctx.service.add_comment(
            ticket.id,
            f"[ASK_USER]\n\n{verdict.comments}",
            author="review",
        )
        outcome = Outcome(State.AWAITING_USER_REPLY, verdict.comments)
        _maybe_cache(ws, input_hash, outcome)
        return outcome

    def _handle_request_changes(
        self,
        verdict: ReviewVerdict,
        ticket: Ticket,
        ctx: StageContext,
        ws: Workspace,
        s: Settings,
        input_hash: str,
        modified_paths: list[str],
        repo_dir: Path,
    ) -> Outcome:
        """Process a REQUEST_CHANGES verdict: round tracking, convergence
        detection, ask splitting, and follow-up spawning.
        """
        rounds = ticket.review_rounds + 1
        ctx.service.set_review_rounds(ticket.id, rounds)

        # Round-cap exhaustion.
        if rounds >= s.review_max_rounds:
            # Emit a diagnostic event so the periodic diagnostic checker
            # can flag tickets that exhausted the review round cap with
            # a potentially stale verdict — this signals a stuck
            # implement/review loop that warrants human inspection.
            try:
                from ..agents.runners.diagnostic_events import emit_diagnostic_event

                emit_diagnostic_event(
                    s,
                    getattr(ticket, "board_id", "") or "",
                    "STALE_REVIEW_VERDICT",
                    ticket.id,
                    (
                        f"Review round cap exhausted at {rounds}/{s.review_max_rounds} "
                        f"REQUEST_CHANGES rounds. Diff may be unchanged across "
                        f"cycles — suspected stale-verdict replay."
                    ),
                    f"{ticket.id}:review-round-cap-exhausted",
                )
            except Exception:
                log.debug(
                    "%s: failed to emit STALE_REVIEW_VERDICT diagnostic event",
                    ticket.id,
                    exc_info=True,
                )
            ctx.service.add_comment(
                ticket.id,
                f"Review round cap exhausted ({rounds}/{s.review_max_rounds} "
                f"REQUEST_CHANGES rounds). Escalating to DELIVERABLE for "
                f"human merge approval.\n\nLast review verdict:\n{verdict.comments}",
                author="review",
            )
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(
                State.DOCUMENTING,
                f"review rounds exhausted ({rounds}/{s.review_max_rounds})",
            )
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        # Convergence detection: repeated findings fingerprint.
        converged = _detect_convergence(verdict, ticket.id, rounds, ws, ctx, input_hash)
        if converged is not None:
            return converged

        # Split asks against the ticket's file_map.
        file_map = _load_file_map(ws)
        in_scope, out_of_scope = _split_asks(verdict.request_changes, file_map)

        already_addressed: list[ReviewAsk] = []
        still_out_of_scope: list[ReviewAsk] = []
        if out_of_scope:
            already_addressed, still_out_of_scope = _gaps_already_addressed(
                out_of_scope, modified_paths
            )

        # Verify any PR/commit claims in the "already addressed" asks.
        if already_addressed:
            truly_addressed, unverified = _verify_already_addressed_asks(
                already_addressed, ticket.id, repo_dir
            )
            already_addressed = truly_addressed
            still_out_of_scope.extend(unverified)

        if already_addressed:
            lines = [
                (
                    f"Review found {len(already_addressed)} gap(s) that appear "
                    "already addressed in the implementer's commits — "
                    "no follow-up needed:"
                ),
                "",
            ]
            for a in already_addressed:
                desc = a.description.splitlines()[0][:120]
                lines.append(f"- {desc}")
            ctx.service.add_comment(ticket.id, "\n".join(lines), author="review")

        if still_out_of_scope:
            new_ids = _spawn_dependency_tickets(ticket, still_out_of_scope, ctx)
            for nid in new_ids:
                ctx.service.set_depends_on(nid, [ticket.id])
            lines = [
                (
                    f"Review found {len(still_out_of_scope)} out-of-scope "
                    "ask(s) — spawned as follow-up ticket(s) that depend on "
                    "this one (they run after it merges):"
                ),
                "",
            ]
            for nid, ask in zip(new_ids, still_out_of_scope, strict=True):
                desc = ask.description.splitlines()[0][:120]
                lines.append(f"- `{nid}` — {desc}")
            ctx.service.add_comment(ticket.id, "\n".join(lines), author="review")

        if in_scope:
            # In-scope changes remain — re-implement just those.
            body = _sanitize_comments(verdict.comments)
            if still_out_of_scope:
                body = (
                    _sanitize_comments(verdict.comments)
                    + "\n\nIn-scope items to fix now (out-of-scope asks were "
                    "spawned as follow-ups):\n"
                    + "\n".join(
                        f"- {a.description.splitlines()[0][:200]}" for a in in_scope
                    )
                )
            ctx.service.add_comment(ticket.id, body, author="review")
            outcome = Outcome(State.READY, verdict.comments)
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        if still_out_of_scope:
            # No in-scope changes: approve so follow-ups can run after merge.
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(
                State.DOCUMENTING,
                f"approved; {len(still_out_of_scope)} out-of-scope "
                "ask(s) spawned as follow-ups",
            )
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        if already_addressed:
            # Every out-of-scope ask was already addressed — approve.
            ctx.service.set_review_rounds(ticket.id, 0)
            outcome = Outcome(
                State.DOCUMENTING,
                f"approved; {len(already_addressed)} review gap(s) "
                "already addressed in the implementer's commits",
            )
            _maybe_cache(ws, input_hash, outcome)
            return outcome

        # REQUEST_CHANGES with no actionable asks — re-implement against
        # the narrative comments.
        ctx.service.add_comment(
            ticket.id, _sanitize_comments(verdict.comments), author="review"
        )
        outcome = Outcome(State.READY, verdict.comments)
        _maybe_cache(ws, input_hash, outcome)
        return outcome
