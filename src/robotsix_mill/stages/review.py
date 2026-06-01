"""Review stage: CODE_REVIEW -> DOCUMENTING | READY | AWAITING_USER_REPLY.

Runs a blind dual-model review of the implementation diff. The review
agent sees ONLY the git diff and ticket spec — no implementation
context.  APPROVE → DOCUMENTING; REQUEST_CHANGES → READY (with review
comments stored); NEEDS_DISCUSSION → AWAITING_USER_REPLY (posts the
verdict as an [ASK_USER] thread; operator's reply auto-resumes review).
"""

from __future__ import annotations

import json
import logging
import re

from ..agents.reviewing import ReviewAsk, ReviewVerdict, run_review_agent
from ..core.models import Ticket
from ..core.states import State
from ..forge.auth import _resolve_remote_url, github_token
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.review")


def _paths_from_diff(diff: str) -> list[str]:
    """Extract modified file paths from a unified git diff.

    Reads `+++ b/<path>` lines (skipping `+++ /dev/null` for deletions),
    deduplicates, preserves first-seen order. Used to pre-seed the
    review agent's message_history with every modified file's content
    so the reviewer doesn't pay for one read_file round-trip per file.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE):
        path = m.group(1).strip()
        if path and path != "/dev/null" and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _load_file_map(ws) -> set[str] | None:
    """Read ``file_map.json`` from the ticket workspace, if any.

    Returns the set of in-scope paths. Returns ``None`` when the file
    is missing, empty, or unparseable — the review stage then treats
    every ask as in-scope (back-compat with tickets that have no
    file map yet, e.g. legacy or scope-free flows).
    """
    p = ws.artifacts_dir / "file_map.json"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    paths = {
        entry["file"] for entry in raw if isinstance(entry, dict) and "file" in entry
    }
    return paths or None


def _file_in_scope(ask_file: str, file_map: set[str]) -> bool:
    """True when *ask_file* refers to the same path as some file_map entry.

    Tolerant of refine vs review path-format mismatches: refine often
    writes short suffixes (``static/board.js``) into file_map while the
    review agent uses canonical repo-relative paths
    (``src/robotsix_mill/runtime/static/board.js``). Exact string
    comparison would mis-classify the latter as out-of-scope and force
    every review ask on that file into a dependency ticket — which is
    how the board ended up with a pile of spurious review-source drafts.

    Match rule:
      1. exact equality, or
      2. one path is a path-suffix of the other (sharing a ``/`` boundary).
    """
    if ask_file in file_map:
        return True
    for m in file_map:
        if ask_file.endswith("/" + m) or m.endswith("/" + ask_file):
            return True
    return False


def _split_asks(
    asks: list[ReviewAsk],
    file_map: set[str] | None,
) -> tuple[list[ReviewAsk], list[ReviewAsk]]:
    """Partition ``asks`` into ``(in_scope, out_of_scope)``.

    An ask is out-of-scope when it touches at least one file NOT in
    ``file_map`` (under :func:`_file_in_scope` semantics — path-suffix
    tolerant, so a refine-side ``static/board.js`` matches a review-side
    ``src/robotsix_mill/runtime/static/board.js``). Asks with empty
    ``files_touched`` are treated as in-scope (file-less clarifications
    stay with the parent). When ``file_map`` is None (legacy /
    scope-free), every ask is in-scope.
    """
    if file_map is None:
        return list(asks), []
    in_scope: list[ReviewAsk] = []
    out_of_scope: list[ReviewAsk] = []
    for ask in asks:
        if not ask.files_touched:
            in_scope.append(ask)
            continue
        if any(not _file_in_scope(f, file_map) for f in ask.files_touched):
            out_of_scope.append(ask)
        else:
            in_scope.append(ask)
    return in_scope, out_of_scope


def _spawn_dependency_tickets(
    parent: Ticket,
    asks: list[ReviewAsk],
    ctx,
) -> list[str]:
    """Materialise each out-of-scope ask as a fresh ticket on the same
    board, return their IDs.

    Title is a short paraphrase of the ask description; body captures
    the full description plus the files the ask would touch so the
    refine agent has enough context to produce a real spec. ``source``
    is ``"review"`` so the operator can trace these back to the
    review pass that spawned them.
    """
    ids: list[str] = []
    for ask in asks:
        title = (
            ask.title.strip() or ask.description.splitlines()[0] or "review follow-up"
        )[:120]
        body_lines = [ask.description.strip()]
        if ask.files_touched:
            body_lines.append("")
            body_lines.append("Files involved:")
            body_lines.extend(f"- `{f}`" for f in ask.files_touched)
        body_lines.append("")
        body_lines.append(
            f"(Spawned by review on parent ticket `{parent.id}` — its "
            "scope did not cover these files.)"
        )
        child = ctx.service.create(
            title,
            "\n".join(body_lines),
            source="review",
            board_id=parent.board_id or None,
        )
        ids.append(child.id)
    return ids


def _build_prior_context(ticket, ctx, ws) -> str | None:
    """Assemble prior review comments and the implement agent's rebuttal
    from the last round into a ``prior-context`` fenced block.

    Returns ``None`` when neither source has content (first review round)."""
    from ..agents.prompt_blocks import section

    parts: list[str] = []

    prior_comments = ctx.service.list_comments(ticket.id)
    if prior_comments:
        # Closed threads are normally skipped (resolved REQUEST_CHANGES
        # feedback the implement agent already addressed). EXCEPTION: a
        # closed top-level [ASK_USER] thread carries the operator's
        # decision from a NEEDS_DISCUSSION pause — the re-run review
        # MUST see it (and its replies) or it will just re-ask the same
        # question and loop. Keep those; drop everything else closed.
        askuser_ids = {
            c.id
            for c in prior_comments
            if c.parent_id is None and c.body.startswith("[ASK_USER]")
        }
        excluded_ids = {
            c.id
            for c in prior_comments
            if c.closed_at is not None and c.id not in askuser_ids
        }
        formatted = "\n".join(
            f"{'  ↳ ' if c.parent_id is not None else ''}[{c.author}] {c.body}"
            for c in prior_comments
            if c.id not in excluded_ids and c.parent_id not in excluded_ids
        )
        if formatted:
            parts.append(section("prior-review-comments", formatted))

    implement_md = ws.artifacts_dir / "implement.md"
    if implement_md.exists():
        parts.append(
            section(
                "implement-rebuttal",
                implement_md.read_text(encoding="utf-8"),
            )
        )

    if not parts:
        return None
    return section("prior-context", "\n\n".join(parts))


class ReviewStage(Stage):
    """Check out the target branch and perform automated code review on the ticket's implemented changes."""

    name = "review"
    input_state = State.CODE_REVIEW
    traced = True

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a CODE_REVIEW ticket: refresh the clone, check out the ticket branch, and run the automated reviewer agent against the diff."""
        s = ctx.settings

        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"

        # Guard: missing clone → BLOCKED (resumable: re-run implement)
        if not (repo_dir / ".git").exists():
            return Outcome(
                State.BLOCKED,
                "no repository clone to review (re-run implement)",
            )

        target_branch = s.forge_target_branch

        # Mint a fresh forge token for the fetch — the clone's baked-in
        # GitHub App installation token expires ~1h after clone time,
        # so by the time review runs (especially after a pause-and-resume
        # cycle) the stale ``origin`` URL would 401 with exit 128.
        # Best-effort: if creds aren't configured we fall back to a
        # tokenless fetch, which still works for public repos.
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        try:
            token = github_token(s, repo_config=ctx.repo_config)
        except RuntimeError:
            token = None

        # Compute diff of all commits on the current branch vs origin/<target>.
        try:
            diff = git_ops.diff_base(
                repo_dir,
                target_branch,
                remote_url=remote_url,
                token=token,
            )
        except Exception as e:
            return Outcome(
                State.BLOCKED,
                f"failed to compute diff: {e}",
            )

        # Empty diff → no-op implementation, approve so deliver can handle it.
        if not diff.strip():
            log.info("%s: empty diff — approving without review", ticket.id)
            return Outcome(State.DOCUMENTING, "empty diff (no-op implementation)")

        spec = ws.read_description()

        prior_context = _build_prior_context(ticket, ctx, ws)

        # Pre-seed the review agent with every modified file's content —
        # the reviewer otherwise burns one read_file round-trip per file
        # to verify claims, which is most of its observation count on
        # any non-trivial diff. See fs_tools.build_preseed_history.
        modified_paths = _paths_from_diff(diff)

        # Run the blind review agent.
        try:
            verdict: ReviewVerdict = run_review_agent(
                settings=s,
                diff=diff,
                spec=spec,
                prior_context=prior_context,
                repo_dir=repo_dir,
                reference_files=modified_paths,
            )
        except Exception as e:
            log.exception("%s: review agent error", ticket.id)
            # Transient model blips (OpenRouter 5xx/429/timeout, the
            # DeepSeek reasoning-400) should get a fresh stage re-run via
            # the worker's stage-retry rather than a hard BLOCK needing a
            # manual resume — same fix as implement.py.
            from ..runtime.transient_errors import reraise_if_transient

            reraise_if_transient(e)
            return Outcome(
                State.BLOCKED,
                f"review agent error — resumable: {e}",
            )

        # Persist review artifact for downstream consumers (e.g. auto-merge).
        ws.artifacts_dir.joinpath("review.md").write_text(
            f"verdict: {verdict.verdict}\n"
            f"auto_merge_eligible: {str(verdict.auto_merge_eligible).lower()}\n",
            encoding="utf-8",
        )

        # Route based on verdict.
        if verdict.verdict == "APPROVE":
            ctx.service.set_review_rounds(ticket.id, 0)
            return Outcome(State.DOCUMENTING, "review approved")
        elif verdict.verdict == "REQUEST_CHANGES":
            rounds = ticket.review_rounds + 1
            ctx.service.set_review_rounds(ticket.id, rounds)
            if rounds >= s.review_max_rounds:
                ctx.service.add_comment(
                    ticket.id,
                    f"Review round cap exhausted ({rounds}/{s.review_max_rounds} "
                    f"REQUEST_CHANGES rounds). Escalating to DELIVERABLE for "
                    f"human merge approval.\n\nLast review verdict:\n{verdict.comments}",
                    author="review",
                )
                ctx.service.set_review_rounds(ticket.id, 0)
                return Outcome(
                    State.DOCUMENTING,
                    f"review rounds exhausted ({rounds}/{s.review_max_rounds})",
                )
            # Split asks against the ticket's file_map. Out-of-scope
            # asks would have made the implement agent edit files
            # outside the declared scope, get bounced by scope-triage
            # next pass, and loop forever. Materialise each as a
            # dependency ticket and park this one on those deps —
            # the unmet-dep gate keeps the parent out of the queue
            # until the new tickets close, then implement runs again
            # with the out-of-scope work already merged into main.
            file_map = _load_file_map(ws)
            in_scope, out_of_scope = _split_asks(
                verdict.request_changes,
                file_map,
            )
            if out_of_scope:
                new_ids = _spawn_dependency_tickets(
                    ticket,
                    out_of_scope,
                    ctx,
                )
                existing = ticket.depends_on or ""
                prior_ids = json.loads(existing) if existing else []
                merged = list(dict.fromkeys(prior_ids + new_ids))
                ctx.service.set_depends_on(ticket.id, merged)
                lines = [
                    f"Review found {len(out_of_scope)} out-of-scope "
                    f"ask(s) — spawned dependency ticket(s) and parked "
                    f"this ticket until they close:",
                    "",
                ]
                for nid, ask in zip(new_ids, out_of_scope):
                    desc = ask.description.splitlines()[0][:120]
                    lines.append(f"- `{nid}` — {desc}")
                ctx.service.add_comment(
                    ticket.id,
                    "\n".join(lines),
                    author="review",
                )
            # Normal in-scope feedback (if any) still goes to the
            # implement agent as a single comment alongside the
            # dep-spawn notice.
            if in_scope or not out_of_scope:
                body = verdict.comments
                if in_scope and out_of_scope:
                    # When mixed: rewrite comments to only the in-scope
                    # subset so implement isn't asked to fix the
                    # out-of-scope work too. The narrative ``comments``
                    # field still covers everything for the operator.
                    body = (
                        verdict.comments + "\n\nIn-scope items (the rest were spawned "
                        "as deps and will be addressed first):\n"
                        + "\n".join(
                            f"- {a.description.splitlines()[0][:200]}" for a in in_scope
                        )
                    )
                ctx.service.add_comment(ticket.id, body, author="review")
            return Outcome(
                State.READY,
                verdict.comments,
            )
        else:  # NEEDS_DISCUSSION
            # A genuine human-decision verdict (e.g. "AC5 vs the 13
            # pre-existing bandit findings — pick one of 3 options").
            # This is NOT an error, so it must NOT BLOCK (which reads
            # as a failure and needs a manual resume). Pause for the
            # operator's reply instead: post the verdict as an
            # [ASK_USER] thread and route to AWAITING_USER_REPLY. When
            # the operator answers and closes the thread,
            # _maybe_resume_awaiting_user_reply auto-resumes the ticket
            # to CODE_REVIEW (paused_from) and review re-runs — this
            # time with the operator's decision visible in
            # prior-context (see _build_prior_context, which keeps
            # closed [ASK_USER] threads).
            ctx.service.add_comment(
                ticket.id,
                f"[ASK_USER]\n\n{verdict.comments}",
                author="review",
            )
            return Outcome(
                State.AWAITING_USER_REPLY,
                verdict.comments,
            )
