"""Refine stage: raw DRAFT -> actionable READY ticket.

Rewrites the file-canonical ``description.md`` into a precise spec the
implement agent can act on unattended; the original draft is kept as an
artifact for traceability. Empty draft or missing OpenRouter key ->
BLOCKED with a clear note (not a crash).

When ``require_approval`` is true (the default), the refined ticket
enters ``human_issue_approval`` instead of ``ready`` — a human must approve
before the implement stage picks it up.

Before the expensive refine agent runs, a cheap **dedup / already-done
check** inspects the draft against existing tickets. If the draft is a
clear duplicate or the change is already covered by a recently-closed
ticket, the ticket is short-circuited to ``CLOSED`` — no refiner, no
human approval gate, no wasted cost.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..agents import dedup
from ..agents import freshness
from ..agents import obsolescence
from ..agents import refining
from ..core.datetime_utils import _as_utc
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..forge.auth import _resolve_remote_url, github_token
from ..runners.pass_runner import load_memory, persist_memory
from ..vcs import git_ops
from .pause import (
    check_for_pause,
    save_conversation_state,
    load_conversation_state,
    clear_conversation_state,
    build_resume_message_history,
    _collect_ask_user_replies,
    acknowledge_unanswered_threads,
)
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.refine")


# Note-prefix constants marking a **non-implementation** closure.
# The dedup guard both *writes* these prefixes (on the DRAFT→DONE
# transition) and *reads* them back in ``_is_valid_dedup_target`` to
# reject a candidate that was itself dedup-/freshness-closed.  Keeping
# them in one place stops the producer and the validator from drifting.
DEDUP_DUPLICATE_PREFIX = "duplicate of "
DEDUP_ALREADY_DONE_PREFIX = "already implemented in "
FRESHNESS_STALE_PREFIX = "stale or invalid finding"
OBSOLESCENCE_GAP_PREFIX = "obsolete — gap already resolved"
NON_IMPLEMENTATION_CLOSE_PREFIXES = (
    DEDUP_DUPLICATE_PREFIX,
    DEDUP_ALREADY_DONE_PREFIX,
    FRESHNESS_STALE_PREFIX,
    OBSOLESCENCE_GAP_PREFIX,
)

UNMERGED_BRANCH_PREFIX = "Implementation exists on branch"

# History-note prefix written by ``TicketService.request_changes`` when an
# operator sends a refined ticket back to DRAFT with feedback. Its presence
# means a human is actively shaping THIS ticket — the dedup guard must not
# then auto-close it as a "duplicate"/"already done" (that silently discards
# the operator's intent; see the auto-mail board-columns ticket that was
# dedup-closed after two rounds of operator "changes requested").
OPERATOR_SENDBACK_PREFIX = "changes requested:"


# Short pointer phrases a refine/conciseness agent sometimes emits in the
# structured spec field *instead of* the actual spec — the real content was
# only in its prose ("…as written above"). Matched against a normalized,
# length-capped string so a genuine (always far longer) spec never trips it.
_PLACEHOLDER_SPEC_PHRASES = (
    "see spec above",
    "see the spec above",
    "see above",
    "see spec",
    "see the spec",
    "see description",
    "see the description",
    "spec above",
    "as above",
    "as written above",
    "see previous",
    "see below",
    "refer to spec",
    "tbd",
    "todo",
)


def _spec_is_degenerate(spec: str | None) -> bool:
    """True when *spec* is empty or a placeholder pointer, not a real spec.

    The refine agent's structured ``spec_markdown`` occasionally collapses
    to a short reference like ``"(see spec above)"`` — non-empty, so the
    bare ``not spec.strip()`` guard misses it, and refine writes the
    pointer straight into the canonical ``description.md`` (blanking the
    ticket body on the board). Treat such degenerate output as "no spec"
    so refine falls back to the original draft instead of clobbering it.

    Only short (≤120-char) single-idea strings can match; a genuine spec
    is much longer, so real content is never dropped.
    """
    text = (spec or "").strip()
    if not text:
        return True
    if len(text) > 120:
        return False
    # Drop markdown/punctuation, collapse whitespace, lowercase.
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())
    if not norm:
        return True
    return any(norm == p or norm.startswith(p + " ") for p in _PLACEHOLDER_SPEC_PHRASES)


def _verify_branch_merged(repo_dir: Path | None, ticket: Ticket) -> bool:
    """Check whether *ticket*'s branch is an ancestor of the base branch.

    When a redrafted ticket has a branch from a prior implement run,
    the refine agent may conclude ``no_change_needed`` because the
    implementation already exists — but if the branch was never merged
    to the base branch, closing as DONE strands the work on an orphaned
    branch.

    Returns ``True`` when the branch is confirmed merged to the base
    branch, or when the check cannot be performed (best-effort: we
    never block a ticket on a transient git error).

    Returns ``False`` only when the branch is confirmed **unmerged**
    — i.e. ``git merge-base --is-ancestor <branch> origin/main`` exits 1.

    When the branch cannot be fetched from origin (e.g. it was committed
    locally by a prior implement run but never pushed), fall back to the
    **local** branch ref ``refs/heads/<branch>`` and check its ancestry
    against ``origin/main``.  Only when neither an origin branch nor a
    local ref can be resolved do we best-effort ``return True`` — there
    is then genuinely nothing to verify.
    """
    if repo_dir is None or not ticket.branch:
        # Nothing to verify — let the no-change-needed pass through.
        return True

    branch = ticket.branch
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "origin", branch],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        # The branch is absent from origin (fetch failed). It may still
        # exist as a local-only ref committed by a prior implement run
        # that never pushed. Fall back to the local ref before allowing
        # the no-change-needed pass-through — otherwise a complete,
        # working feature strands on an orphaned local WIP commit.
        log.debug(
            "%s: cannot fetch branch '%s' from origin — "
            "falling back to local ref for merge check",
            ticket.id,
            branch,
        )
        local_ref = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
        )
        if local_ref.returncode != 0:
            # Neither an origin branch nor a local ref — nothing to
            # verify, best-effort allow.
            log.debug(
                "%s: branch '%s' resolves on neither origin nor locally "
                "— allowing no-change-needed (best-effort)",
                ticket.id,
                branch,
            )
            return True
        local_check = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "merge-base",
                "--is-ancestor",
                f"refs/heads/{branch}",
                "origin/main",
            ],
            capture_output=True,
            text=True,
        )
        if local_check.returncode == 0:
            return True  # local branch is merged
        if local_check.returncode == 1:
            log.info(
                "%s: local branch '%s' is NOT an ancestor of origin/main "
                "— implementation unmerged",
                ticket.id,
                branch,
            )
            return False  # local branch is unmerged
        # Any other exit code (git error) — best-effort, don't block.
        log.debug(
            "%s: local merge-base check failed for branch '%s' — "
            "allowing no-change-needed (best-effort)",
            ticket.id,
            branch,
        )
        return True

    # git merge-base --is-ancestor: exit 0 = is ancestor, 1 = not ancestor
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "merge-base",
            "--is-ancestor",
            f"origin/{branch}",
            "origin/main",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True  # branch is merged
    if result.returncode == 1:
        log.info(
            "%s: branch '%s' is NOT an ancestor of main — implementation unmerged",
            ticket.id,
            branch,
        )
        return False  # branch is unmerged

    # Any other exit code (git error) — best-effort, don't block.
    log.debug(
        "%s: merge-base check failed for branch '%s' — "
        "allowing no-change-needed (best-effort)",
        ticket.id,
        branch,
    )
    return True


def _resolve_next_state(
    ctx: StageContext,
    spec: str,
    ticket_id: str,
    source: str | None = None,
) -> tuple[State, str | None]:
    """Return (next_state, auto_approve_note_or_None).

    Encapsulates the decision: if approval is not required → READY;
    if auto-approve is disabled → HUMAN_ISSUE_APPROVAL; otherwise run
    the auto-approve triage on the spec → READY on APPROVE (no design
    decision found), HUMAN_ISSUE_APPROVAL otherwise (or on error).
    Empty/whitespace specs skip the triage entirely and go to
    HUMAN_ISSUE_APPROVAL when gated, mirroring the original behaviour.

    Test-gap tickets (``source == "test_gap"``) auto-approve
    deterministically — they only add test coverage with no
    production-code change, so there's no design decision a human
    could meaningfully veto. Three triage runs on test-gap tickets
    on 2026-05-28 all fell back to human approval and were
    rubber-stamped, so the LLM hop was pure toil + cost. Short-
    circuit before the LLM call.

    Every triage outcome carries a structured note so the auto-approve
    decision is visible in ticket history.  Triage failures or
    unexpected errors in note assembly fall through to
    HUMAN_ISSUE_APPROVAL with a fallback note — the transition is
    never blocked.
    """
    if not ctx.settings.require_approval:
        return State.READY, None
    if _spec_is_degenerate(spec):
        return State.HUMAN_ISSUE_APPROVAL, None
    if not ctx.settings.auto_approve_enabled:
        return State.HUMAN_ISSUE_APPROVAL, None
    # Deterministic auto-approve for sources whose drafts are
    # internal-only by construction: they're proposed by mill's own
    # periodic agents (audit, agent_check, bc_check, …) whose scope
    # is dead-code removal, prompt updates, memory ledger structure,
    # config cleanup, docstring additions — no behavioural risk a
    # human reviewer can meaningfully veto. test_gap (the original
    # rule in 28a6b02) joins the same family. Three rounds of
    # rubber-stamping all 21+ tickets from these sources without
    # rejection (see 09cc) made the LLM hop pure toil.
    _AUTO_APPROVE_SOURCES = {
        "test_gap",
        "audit",
        "agent_check",
        "bc_check",
        "completeness_check",
        "module_curator",
        "copy_paste",
    }
    if source in _AUTO_APPROVE_SOURCES:
        return State.READY, (
            f"auto-approve: APPROVE — {source} (deterministic rule: "
            "mill-internal periodic-agent proposal, no design risk)"
        )
    try:
        result = refining.triage_auto_approve(
            settings=ctx.settings,
            spec=spec,
        )
        if result.decision == "APPROVE":
            return State.READY, f"auto-approve: APPROVE — {result.reason}"
        # NEEDS_APPROVAL — return the reason as a structured history
        # note (no side-effect comment; this is the sole surface).
        return (
            State.HUMAN_ISSUE_APPROVAL,
            f"auto-approve: NEEDS_APPROVAL — {result.reason}",
        )
    except Exception:
        log.warning(
            "auto-approve triage failed, falling back to human approval",
            exc_info=True,
        )
    return (
        State.HUMAN_ISSUE_APPROVAL,
        "auto-approve: triage failed — falling back to human approval",
    )


def _build_candidates_block(candidates: list[Ticket], ctx: StageContext) -> str:
    """Render candidates for the dedup check as one Markdown section
    per ticket.

    The previous implementation emitted a single JSON blob which is
    fine for the model but renders as an unreadable wall of escaped
    JSON in Langfuse's prompt viewer when an operator audits the run.
    The new format is one ``## <id>`` heading per candidate followed
    by a short metadata list and a fenced ``body`` block so each
    ticket is a readable section both inline and in the trace UI.

    Each rendered candidate carries the same fields the dedup yaml
    asks for (``id``, ``title``, ``state``, ``source``, ``body``);
    only the encoding changed.
    """
    if not candidates:
        return "(no candidates)"
    from ..core.text_utils import truncate_at_boundary

    max_chars = ctx.settings.dedup_candidate_body_max_chars
    sections: list[str] = []
    for t in candidates:
        try:
            body = ctx.service.workspace(t).read_description()
        except Exception:
            body = ""
        from ..agents.prompt_blocks import section as _section

        body = body.strip()
        # Bound pathologically long candidate bodies so they can't blow
        # up the dedup prompt. ``max_chars <= 0`` disables truncation
        # (and guards the ``truncate_at_boundary(text, 0) == ""`` edge).
        if max_chars > 0:
            body = truncate_at_boundary(body, max_chars)
        meta = (
            f"## {t.id}\n"
            f"- title: {t.title}\n"
            f"- state: {t.state.value}\n"
            f"- source: {t.source}\n"
        )
        sections.append(meta + "\n" + _section("body", body))
    return "\n\n".join(sections)


class RefineStage(Stage):
    """Refine a draft ticket into a detailed, self-contained engineering specification."""

    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a DRAFT ticket: gate on dependencies, refine the draft into a self-contained engineering spec (or split into children / promote to epic) via the refining agent."""
        ws = ctx.service.workspace(ticket)
        draft = ws.read_description().strip()
        epic_ctx = ctx.service.get_epic_context(ticket)
        title = ticket.title.strip()
        if not title and not draft:
            return Outcome(State.BLOCKED, "empty title and draft — nothing to refine")

        # --- dependency gate: refuse to refine until all deps are
        # terminal (CLOSED/DONE). Same-state no-op → the reconcile
        # sweep re-enqueues this ticket each poll cycle.
        unmet = ctx.service.unmet_dependencies(ticket)
        if unmet:
            log.debug(
                "%s: unmet dependencies — deferring refine: %s",
                ticket.id,
                unmet,
            )
            return Outcome(State.DRAFT)

        s = ctx.settings

        # Phase 1: build the workspace. A meta-board ticket is cross-repo:
        # a triage agent picks the required registered repos and we clone
        # those into a multi-repo workspace (repo_dir = first, extra_roots =
        # all), so the refine agent can read across them. Every other board
        # is the normal single-repo clone.
        extra_roots: list[Path] | None = None
        if ticket.board_id == "meta":
            from ..meta.workspace import build_triaged_meta_workspace

            repo_dir, extra_roots, outcome = build_triaged_meta_workspace(
                ctx, ticket, ws, draft, author="refine"
            )
            if outcome is not None:
                return outcome
        else:
            result = RefineStage._clone_or_resume(ctx, ticket, ws)
            if isinstance(result, Outcome):
                return result
            repo_dir = result

        # --- prepare hook: let the repo run custom setup after clone,
        # before any agent executes ---
        if repo_dir is not None:
            from ..hooks import run_prepare_hook

            hook_error = run_prepare_hook(repo_dir, ticket.id, ws.dir)
            if hook_error is not None:
                return Outcome(State.BLOCKED, hook_error)

        # Phase 2: freshness gate — verify cited evidence against HEAD
        # before spending any LLM budget on refine.  Runs before the
        # dedup guard because it is deterministic (no LLM call).
        stale = RefineStage._run_freshness_gate(ctx, ticket, draft, repo_dir, s)
        if stale is not None:
            return stale

        # Phase 2.5: obsolescence gate — for *spawned* follow-up drafts,
        # re-evaluate (via a cheap LLM call) whether the cited gap was
        # already resolved in place by a parallel/parent ticket.  Runs
        # after the deterministic freshness gate and before the dedup
        # guard.
        obsolete = RefineStage._run_obsolescence_gate(ctx, ticket, draft, repo_dir, s)
        if obsolete is not None:
            return obsolete

        # Phase 3: dedup guard
        dup = RefineStage._run_dedup_guard(ctx, ticket, draft, repo_dir, s)
        if dup is not None:
            return dup

        # Phase 3.5: advisory dedup against CONCURRENT in-flight tickets.
        # The dedup guard above can only close against a genuinely-DONE
        # candidate, so two drafts that converge while both in flight both
        # survive it.  This best-effort pass flags (never closes) such an
        # overlap so refine/the operator can decide.
        draft = RefineStage._run_inflight_advisory(ctx, ticket, draft, ws, s)

        # Phase 4: refine agent + result handling
        return RefineStage._run_refine_agent(
            ctx, ticket, draft, repo_dir, epic_ctx, title, ws, s, extra_roots
        )

    @staticmethod
    def _clone_or_resume(
        ctx: StageContext, ticket: Ticket, ws
    ) -> Path | Outcome | None:
        """Resolve remote URL, reuse or clone repo, escalate clone failures.

        Returns the ``repo_dir`` ``Path`` when a clone exists or is
        successfully created.  On clone failure, adds a BLOCKED comment
        via ``ctx.service.add_comment`` and returns an ``Outcome``.
        Returns ``None`` when no ``remote_url`` is configured (caller
        treats ``None`` as "no repo available").
        """
        s = ctx.settings
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        if not remote_url:
            return None

        cand = ws.dir / "repo"
        if (cand / ".git").exists():
            return cand  # idempotent: reuse an existing clone

        try:
            try:
                token = github_token(s, repo_config=ctx.repo_config)
            except RuntimeError:
                token = None  # no credentials configured — clone will fail
            git_ops.clone(
                remote_url,
                cand,
                s.forge_target_branch,
                token,
            )
            return cand
        except subprocess.CalledProcessError as e:
            # Escalate clone failure to BLOCKED — running refine
            # with no repo grounds the agent's system prompt
            # against tools that aren't registered (the
            # `tools=[]` path in refining.py:385). The result is
            # an inconsistent, tool-less refine that wastes
            # tokens. Surface the cause to the operator instead.
            reason = f"refine clone failed: {(e.stderr or '').strip()[:200]}"
            log.warning("%s: %s", ticket.id, reason)
            # The diagnostic used to be posted as a comment; the
            # transition note carries the same info and v1 keeps
            # agent conclusions out of comments. The remediation
            # hint ("fix permissions/credentials/disk/network, then
            # resume-blocked") lives in this commit's git log as
            # ambient context.
            return Outcome(
                State.BLOCKED,
                f"{reason}. Fix the underlying cause (permissions, "
                "credentials, disk space, network) then "
                "`resume-blocked` to re-run refine.",
            )

    @staticmethod
    def _run_dedup_guard(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        s,
    ) -> Outcome | None:
        """Run the dedup / already-done guard (best-effort).

        Returns ``None`` for trivial drafts (< 100 chars) or when the
        dedup check finds no match, signalling that refine should
        proceed.  Returns ``Outcome(State.DONE, ...)`` when the verdict
        indicates a duplicate or already-implemented ticket.
        """
        if len(draft) < 100:
            log.debug(
                "%s: trivial draft (%d chars), skipping dedup",
                ticket.id,
                len(draft),
            )
            return None

        # Operator-iteration guard: if a human has sent this ticket back with
        # "changes requested" feedback, they are actively shaping it — do NOT
        # let dedup auto-close it as a duplicate/already-done (that discards
        # their intent). Proceed straight to refine instead.
        try:
            if any(
                ev.note and ev.note.startswith(OPERATOR_SENDBACK_PREFIX)
                for ev in ctx.service.history(ticket.id)
            ):
                log.info(
                    "%s: operator has requested changes — skipping dedup guard "
                    "(human is actively iterating this ticket)",
                    ticket.id,
                )
                return None
        except Exception:  # noqa: BLE001 — best-effort; never block refine
            log.debug("%s: dedup sendback-check skipped", ticket.id, exc_info=True)

        # Gather candidate tickets: all non-terminal + recently closed.
        all_tickets = ctx.service.list()
        now = datetime.now(timezone.utc)
        lookback_cutoff = datetime.fromtimestamp(
            now.timestamp() - s.dedup_lookback_days * 86400, tz=timezone.utc
        )
        non_terminal = {State.CLOSED, State.ERRORED}
        candidates = [
            t
            for t in all_tickets
            if t.id != ticket.id
            and (
                t.state not in non_terminal
                or (
                    t.state == State.CLOSED and _as_utc(t.updated_at) >= lookback_cutoff
                )
            )
        ]
        # Pre-filter by parent/epic: when the ticket belongs to an epic,
        # only keep candidates that share the same parent, are the parent
        # itself, are orphans (no parent), or are recently-closed
        # cross-epic tickets.  This avoids feeding the LLM dozens of
        # tickets from unrelated areas.
        if ticket.parent_id is not None:
            before = len(candidates)
            candidates = [
                t
                for t in candidates
                if t.parent_id == ticket.parent_id  # sibling
                or t.id == ticket.parent_id  # parent epic
                or t.parent_id is None  # orphan
                or t.state == State.CLOSED  # recently-closed (any area)
            ]
            if len(candidates) < before:
                log.debug(
                    "%s: parent filter reduced dedup candidates from %d to %d",
                    ticket.id,
                    before,
                    len(candidates),
                )

        # Epics are never duplicates of task/inquiry tickets.
        # Keep the parent epic (already handled above) but drop all others.
        candidates = [
            t for t in candidates if t.kind != "epic" or t.id == ticket.parent_id
        ]

        # Similarity-based pre-filter: keep only the top-N most relevant
        # candidates so the LLM sees a fixed budget regardless of repo size.
        before_ranking = len(candidates)
        candidates = dedup.rank_candidates_by_similarity(
            draft_title=ticket.title,
            draft_body=draft,
            candidates=candidates,
            max_candidates=s.dedup_max_candidates,
        )
        if len(candidates) < before_ranking:
            log.debug(
                "%s: similarity ranking reduced dedup candidates from %d to %d",
                ticket.id,
                before_ranking,
                len(candidates),
            )

        candidates_json = _build_candidates_block(candidates, ctx)

        # Zero-overlap short-circuit: when the draft shares no meaningful
        # token with any candidate (title+body), no candidate could
        # plausibly be a duplicate — skip the LLM call entirely. Bodies
        # are assembled the same way _build_candidates_block reads them.
        candidate_texts: list[str] = []
        for t in candidates:
            try:
                body = ctx.service.workspace(t).read_description()
            except Exception:
                body = ""
            candidate_texts.append(f"{t.title} {body}")

        if s.dedup_skip_on_no_overlap and (
            not candidates
            or not dedup.any_candidate_overlap(
                draft_title=ticket.title,
                draft_body=draft,
                candidates_texts=candidate_texts,
            )
        ):
            log.debug(
                "%s: no candidate token overlap (%d candidates) — "
                "skipping dedup LLM call",
                ticket.id,
                len(candidates),
            )
            verdict = {
                "duplicate_of": None,
                "already_done": None,
                "reason": "skipped: no candidate token overlap",
            }
        else:
            try:
                verdict = dedup.run_dedup_check(
                    settings=s,
                    draft_title=ticket.title,
                    draft_body=draft,
                    candidates_json=candidates_json,
                    repo_dir=repo_dir,
                )
            except Exception:
                log.warning(
                    "%s: dedup check failed, proceeding with refine",
                    ticket.id,
                    exc_info=True,
                )
                verdict = {
                    "duplicate_of": None,
                    "already_done": None,
                    "reason": "dedup check failed",
                }

        # Discarded drafts go to DONE (not directly CLOSED) so retrospect
        # still analyses them — sanity-check the dedup verdict, capture
        # any lesson in the memory ledger, and keep the audit trail
        # consistent with every other terminal-ish ticket.
        dup_id = verdict.get("duplicate_of")
        if dup_id:
            if RefineStage._is_valid_dedup_target(ctx, ticket, dup_id, repo_dir):
                return Outcome(
                    State.DONE,
                    f"{DEDUP_DUPLICATE_PREFIX}{dup_id}: {verdict.get('reason', 'no reason')}",
                )
            log.info(
                "%s: dedup verdict named duplicate_of=%s but it is not a "
                "valid dedup target (terminal/declined/circular/unmerged) — "
                "proceeding with refine",
                ticket.id,
                dup_id,
            )
        done_id = verdict.get("already_done")
        if done_id:
            if RefineStage._is_valid_dedup_target(ctx, ticket, done_id, repo_dir):
                return Outcome(
                    State.DONE,
                    f"{DEDUP_ALREADY_DONE_PREFIX}{done_id}: {verdict.get('reason', 'no reason')}",
                )
            log.info(
                "%s: dedup verdict named already_done=%s but it is not a "
                "valid dedup target (terminal/declined/circular/unmerged) — "
                "proceeding with refine",
                ticket.id,
                done_id,
            )
        return None

    @staticmethod
    def _is_valid_dedup_target(
        ctx: StageContext,
        ticket: Ticket,
        candidate_id: str,
        repo_dir: Path | None,
    ) -> bool:
        """Return whether *candidate_id* is an acceptable dedup target
        for *ticket*.

        Best-effort: any lookup failure degrades to ``True`` is wrong —
        a failure should *not* close the ticket, so it returns ``False``
        only for proven-bad candidates and ``True`` otherwise.  A
        candidate that cannot be resolved to a ticket (e.g. a commit
        hash for ``already_done``) is accepted.

        Rejects (returns ``False``):
        - a **circular** target whose history marks it as a dedup of
          ``ticket`` itself;
        - an ``ERRORED`` candidate (failed attempt);
        - a ``CLOSED`` candidate that never passed through ``DONE``
          (declined-as-noise / split parent);
        - a candidate that reached ``DONE`` via a non-implementation
          closure (dedup-closed or freshness-closed — never actually
          implemented);
        - a candidate whose implementation branch was never merged to
          the base branch (stranded implementation).
        """
        try:
            cand = ctx.service.get(candidate_id)
            if cand is None:
                # Not a ticket id (e.g. a commit hash) — preserve the
                # already-implemented-via-commit behaviour.
                return True
            history = ctx.service.history(cand.id)

            # Circular guard: the candidate was itself closed as a
            # dedup of the current ticket.
            _dedup_prefixes = (DEDUP_DUPLICATE_PREFIX, DEDUP_ALREADY_DONE_PREFIX)
            for ev in history:
                note = ev.note or ""
                if note.startswith(_dedup_prefixes) and ticket.id in note:
                    return False

            # Failed attempt — let refine re-escalate.
            if cand.state == State.ERRORED:
                return False

            # Declined-as-noise / split parent: CLOSED but never DONE.
            if cand.state == State.CLOSED and not any(
                ev.state == State.DONE for ev in history
            ):
                return False

            # Reached DONE via a non-implementation closure (dedup- or
            # freshness-closed) — never actually implemented.  Applies
            # whether the candidate is still DONE or has since CLOSED.
            for ev in history:
                if ev.state == State.DONE and (ev.note or "").startswith(
                    NON_IMPLEMENTATION_CLOSE_PREFIXES
                ):
                    return False

            # The candidate reached DONE via a real implementation, but if
            # that implementation lives only on an unmerged branch the work
            # never reached main — "already implemented in X" is invalid.
            if cand.branch and not _verify_branch_merged(repo_dir, cand):
                log.info(
                    "%s: dedup candidate %s has branch '%s' not merged to "
                    "main — not a valid dedup target",
                    ticket.id,
                    candidate_id,
                    cand.branch,
                )
                return False

            return True
        except Exception:
            # Best-effort: a lookup error must never raise and must not
            # close the ticket — degrade to "proceed with refine".
            log.warning(
                "%s: dedup target validation failed for %s, proceeding with refine",
                ticket.id,
                candidate_id,
                exc_info=True,
            )
            return False

    @staticmethod
    def _run_inflight_advisory(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        ws,
        s,
    ) -> str:
        """Advisory pre-refine dedup against CONCURRENT in-flight tickets.

        Best-effort: reuses ``dedup.find_inflight_overlap`` to spot a
        recent matching ticket in ANY state (including
        DRAFT/READY/REFINING/IMPLEMENT — the structural gap the dedup
        guard cannot close).  On a strong match, logs a warning and
        prepends a ``[!warning]`` advisory naming the concurrent ticket
        to the draft — it never auto-closes, mirroring c853's
        epic-decomposition pattern; refine/the operator decides.

        Returns the (possibly annotated) draft to carry forward to the
        refine agent.  Independent drafts only: epic children get their
        concurrent-overlap flag at epic-decomposition pre-filing time, so
        children/epics are skipped.  Trivial drafts (< 100 chars) skip the
        check, matching the dedup guard's threshold.
        """
        if ticket.parent_id is not None or ticket.kind == "epic":
            return draft
        if len(draft) < 100:
            return draft

        from ..dedup import annotate_child_body, find_inflight_overlap

        note = find_inflight_overlap(
            ctx.service,
            ticket.id,
            ticket.title,
            draft,
            s,
            datetime.now(timezone.utc),
        )
        if not note:
            return draft

        log.warning(
            "%s: draft flagged as possible duplicate of a concurrent "
            "in-flight ticket — %s",
            ticket.id,
            note,
        )
        annotated = annotate_child_body(
            draft, note, source_desc="draft-intake pre-refine dedup"
        )
        new_hash = ws.write_description(annotated)
        ctx.service.set_content_hash(ticket.id, new_hash)
        return annotated

    @staticmethod
    def _run_freshness_gate(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        s,
    ) -> Outcome | None:
        """Run the deterministic freshness gate (best-effort).

        Returns ``None`` when the cited evidence is confirmed fresh or
        the gate is disabled / not applicable, signalling that refine
        should proceed.  Returns ``Outcome(State.DONE, ...)`` when the
        cited evidence cannot be verified on HEAD — the ticket is stale
        or hallucinated and should be short-circuited.

        The gate is gated behind ``freshness_gate_enabled`` (default
        ``False``, opt-in).  When enabled, it extracts file paths from
        the draft and verifies them against the cloned repo.  If the
        draft cites multiple files and the majority cannot be found,
        the ticket is likely stale.
        """
        if not s.freshness_gate_enabled:
            return None

        if not draft or len(draft) < 50:
            log.debug(
                "%s: trivial draft (%d chars), skipping freshness gate",
                ticket.id,
                len(draft),
            )
            return None

        try:
            result = freshness.run_freshness_check(
                draft=draft,
                repo_dir=repo_dir,
            )
        except Exception:
            log.warning(
                "%s: freshness check failed, proceeding with refine",
                ticket.id,
                exc_info=True,
            )
            return None

        if result.get("stale"):
            reason = result.get("reason", "cited evidence not found on HEAD")
            log.info(
                "%s: freshness gate flagged as stale — %s",
                ticket.id,
                reason,
            )
            # Discarded drafts go to DONE so retrospect still analyses
            # them — same pattern as the dedup guard.
            return Outcome(
                State.DONE,
                f"{FRESHNESS_STALE_PREFIX} — {reason}",
            )

        log.debug(
            "%s: freshness gate passed — %s",
            ticket.id,
            result.get("reason", ""),
        )
        return None

    @staticmethod
    def _run_obsolescence_gate(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        s,
    ) -> Outcome | None:
        """Run the LLM-based obsolescence gate (best-effort).

        For a *spawned* follow-up/corrective draft, re-evaluate whether
        the cited evidence gap (a missing doc section, a still-listed
        dependency, a grep that should return nothing) still exists on
        HEAD.  When the gap was already resolved in place by a
        parallel/parent ticket, short-circuit the draft straight to
        ``DONE`` before any refine LLM budget is spent.

        Returns ``None`` (proceed) when the gate is disabled, the draft
        is trivial, the ticket is user-authored, the check fails, or the
        gap is confirmed to still exist.  Returns
        ``Outcome(State.DONE, ...)`` when the gap is already resolved.

        The gate is gated behind ``obsolescence_gate_enabled`` (default
        ``False``, opt-in).  User-authored drafts reflect deliberate
        human intent and are never auto-closed — the gate targets the
        spawned follow-up/corrective drafts (retrospect, agent,
        review-spawned) that make up the Evidence population.
        """
        if not s.obsolescence_gate_enabled:
            return None

        if not draft or len(draft) < 50:
            log.debug(
                "%s: trivial draft (%d chars), skipping obsolescence gate",
                ticket.id,
                len(draft),
            )
            return None

        if ticket.source == SourceKind.USER:
            log.debug(
                "%s: user-authored draft, skipping obsolescence gate",
                ticket.id,
            )
            return None

        try:
            result = obsolescence.run_obsolescence_check(
                settings=s,
                draft_title=ticket.title,
                draft_body=draft,
                repo_dir=repo_dir,
            )
        except Exception:
            log.warning(
                "%s: obsolescence check failed, proceeding with refine",
                ticket.id,
                exc_info=True,
            )
            return None

        if result.get("obsolete"):
            reason = result.get("reason", "cited gap already resolved on HEAD")
            log.info(
                "%s: obsolescence gate flagged as obsolete — %s",
                ticket.id,
                reason,
            )
            # Discarded drafts go to DONE so retrospect still analyses
            # them — same pattern as the freshness/dedup gates.
            return Outcome(
                State.DONE,
                f"{OBSOLESCENCE_GAP_PREFIX} — {reason}",
            )

        log.debug(
            "%s: obsolescence gate passed — %s",
            ticket.id,
            result.get("reason", ""),
        )
        return None

    @staticmethod
    def _run_refine_agent(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        epic_ctx: dict | None,
        title: str,
        ws,
        s,
        extra_roots: list[Path] | None = None,
    ) -> Outcome:
        """Run the full refine-agent pipeline and handle the result.

        Covers split-child fast-path, reviewer-comment collection,
        triage skip, agent invocation, pause detection, artifact
        persistence, spec review, single-scope and multi-scope split
        outcomes.
        """
        # --- skip re-refinement for split children ---
        # A child ticket created from a split already has a refined
        # spec in its description.md.  Detect this by checking whether
        # the parent is CLOSED with a "split into" note — the canonical
        # signal that this ticket's description is already the refined
        # output.  When children are reparented to an umbrella epic
        # the direct parent is no longer CLOSED, so also check the
        # ticket's own history for a "split from" transition note.
        # We must NOT short-circuit for retrospect-spawned drafts
        # (whose parent is also CLOSED but for a different reason and
        # whose description is a raw draft, not a spec).
        is_split_child = False
        if ticket.parent_id is not None:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.state == State.CLOSED:
                # Only short-circuit if the parent was closed by a
                # split — otherwise (e.g. retrospect spawn) the
                # draft still needs refinement.
                parent_history = ctx.service.history(parent.id)
                is_split_child = any(
                    ev.state == State.CLOSED
                    and ev.note
                    and ev.note.startswith("split into")
                    for ev in parent_history
                )
        if not is_split_child:
            # Fallback: check the ticket's own history for a
            # "split from" note (children reparented to an epic).
            own_history = ctx.service.history(ticket.id)
            is_split_child = any(
                ev.note and ev.note.startswith("split from") for ev in own_history
            )
        if is_split_child:
            spec = draft
            if not spec.strip():
                return Outcome(State.BLOCKED, "split child has empty description")
            # Preserve the raw draft if not already preserved.
            draft_original = ws.artifacts_dir / "draft-original.md"
            if not draft_original.exists():
                draft_original.write_text(
                    "(split child — spec written by parent's refine agent)",
                    encoding="utf-8",
                )
            # Split children skip the refine agent — but implement still
            # demands a file_map.json. Write an empty one so the
            # downstream gate treats this as scope-free mode rather
            # than "refine broken" → BLOCKED.
            file_map_path = ws.artifacts_dir / "file_map.json"
            if not file_map_path.exists():
                file_map_path.write_text("[]", encoding="utf-8")
            next_state, auto_note = _resolve_next_state(
                ctx, spec, ticket.id, source=ticket.source
            )
            note = "split child — spec already refined"
            if auto_note:
                note += f" | {auto_note}"
            return Outcome(next_state, note)

        # --- maintenance triage: route operational requests to the
        # maintenance agent, bypassing refine + implement ---
        if s.maintenance_triage_enabled:
            # Phase A — deterministic keyword check (cheap, no LLM)
            kw_match = refining._check_maintenance_keywords(title, draft)
            if kw_match:
                return Outcome(
                    State.MAINTENANCE,
                    f"maintenance triage: {kw_match}",
                )
            # Phase B — LLM triage for drafts without explicit keywords
            try:
                triage = refining.triage_maintenance(
                    settings=s,
                    title=title,
                    draft=draft,
                    repo_dir=repo_dir,
                    extra_roots=extra_roots,
                )
                if triage.decision == "MAINTENANCE":
                    return Outcome(
                        State.MAINTENANCE,
                        f"maintenance triage: {triage.reason}",
                    )
            except Exception:
                log.warning(
                    "%s: maintenance triage failed, falling through to "
                    "normal refine",
                    ticket.id,
                    exc_info=True,
                )
        # --- end maintenance triage ---

        # --- gather reviewer comments (sendback guard) ---
        # ``mill`` and ``system`` author comments (trace-link auto-posts
        # from runtime.worker._post_trace_comment; timeout-escalation
        # pings) are diagnostic notes, not human feedback. Including
        # them taught refine to treat an inaccessible Langfuse URL as
        # reviewer comments and ask_user what the reviewer said.
        _NON_FEEDBACK_AUTHORS = {"mill", "system"}
        reviewer_comments: str | None = None
        open_thread_ids: set[int] = set()
        try:
            comments = ctx.service.list_comments(ticket.id)
            if comments:
                # Only count non-closed, non-system top-level threads
                # for sendback detection.
                open_threads = [
                    c
                    for c in comments
                    if c.parent_id is None
                    and c.closed_at is None
                    and c.author not in _NON_FEEDBACK_AUTHORS
                ]
                if open_threads:
                    open_thread_ids = {c.id for c in open_threads}
                    closed_ids = {c.id for c in comments if c.closed_at is not None}
                    reviewer_comments = "\n".join(
                        f"[id={c.id} @ {c.created_at.isoformat()}] {c.body}"
                        for c in comments
                        if c.id not in closed_ids
                        and c.parent_id not in closed_ids
                        and c.author not in _NON_FEEDBACK_AUTHORS
                    )
                    if not reviewer_comments:
                        reviewer_comments = None
        except Exception:
            log.warning("%s: list_comments failed, proceeding without", ticket.id)

        # --- triage: skip full refine for already-precise drafts ---
        # A single cheap LLM call classifies the draft.  If it's
        # already a precise, implementation-ready spec, skip the
        # expensive refine agent entirely.  ONLY skip when:
        # - the feature flag is enabled, AND
        # - no reviewer sendback (human-flagged changes always refine), AND
        # - the triage model says SKIP.
        if s.refine_triage_enabled and not reviewer_comments:
            try:
                triage = refining.triage_refine(
                    settings=s,
                    title=title,
                    draft=draft,
                    repo_dir=repo_dir,
                    extra_roots=extra_roots,
                )
                if triage.decision == "SKIP":
                    # The draft IS the spec — preserve it unchanged.
                    (ws.artifacts_dir / "draft-original.md").write_text(
                        draft if draft else "(title-only ticket, no body provided)",
                        encoding="utf-8",
                    )
                    # Try to extract backtick-quoted file paths from
                    # the draft so the implement stage can enforce
                    # scope even when we skip the refine agent.
                    # Pattern: backtick-quoted strings that look like
                    # file paths (contain a '/' directory separator
                    # and a file extension).
                    _PATH_RE = re.compile(r"`([^`]*/[^`]*\.[a-zA-Z]{1,10})`")
                    extracted = _PATH_RE.findall(draft)
                    if extracted:
                        file_map_path = ws.artifacts_dir / "file_map.json"
                        if not file_map_path.exists():
                            file_map_path.write_text(
                                json.dumps(
                                    [
                                        {"file": p, "note": "from draft"}
                                        for p in extracted
                                    ],
                                    indent=2,
                                ),
                                encoding="utf-8",
                            )
                        next_state, auto_note = _resolve_next_state(
                            ctx,
                            draft,
                            ticket.id,
                            source=ticket.source,
                        )
                        note = f"triage SKIP: {triage.reason}"
                        if auto_note:
                            note += f" | {auto_note}"
                        return Outcome(next_state, note)
                    # No paths extracted — fall through to the refine
                    # agent (do NOT write an empty file_map).  The
                    # refine agent will explore the codebase and
                    # produce a proper file_map with real file
                    # exploration behind it.
                    log.info(
                        "%s: triage SKIP but no file paths in draft "
                        "— falling through to refine agent for "
                        "file_map",
                        ticket.id,
                    )
            except Exception:
                log.warning(
                    "%s: triage failed, falling through to full refine",
                    ticket.id,
                    exc_info=True,
                )
        # --- end triage ---

        # --- run the refine agent ---
        # Meta tickets have no registered repo_config; their memory ledger
        # is keyed on the ticket's own board_id ("meta"). Every other board
        # uses its repo_config.board_id.
        memory_board_id = (
            ctx.repo_config.board_id if ctx.repo_config else ticket.board_id
        )
        refine_memory_path = s.memory_file_for("refine", memory_board_id)
        memory_text = load_memory(refine_memory_path, max_chars=s.max_memory_chars)

        # extra_roots is passed in (non-empty for meta-board multi-repo
        # workspaces; None for the normal single-repo path).

        # --- resume awareness: detect if returning from a pause ---
        resume_history: list | None = None
        saved_state = load_conversation_state(ws, "refine")
        if saved_state is not None:
            # Check whether the ticket is resuming from a pause by
            # looking for a prior AWAITING_USER_REPLY event in the
            # ticket history.
            own_history = ctx.service.history(ticket.id)
            was_paused = any(
                ev.state == State.AWAITING_USER_REPLY for ev in own_history
            )
            if was_paused:
                # Collect operator replies from every closed [ASK_USER]
                # thread.  The agent may have asked multiple questions
                # across pause/resume cycles; each answered question
                # contributes its replies.
                reply_text = _collect_ask_user_replies(ctx, ticket)
                resume_history = build_resume_message_history(
                    saved_state,
                    reply_text,
                )
                log.info(
                    "%s: resuming refine from pause — "
                    "loaded %d-byte conversation state",
                    ticket.id,
                    len(saved_state),
                )

        from ..repo_settings import resolve_language_instructions

        language_instructions = resolve_language_instructions(s, repo_dir)
        try:
            result = refining.run_refine_agent(
                settings=s,
                title=ticket.title,
                draft=draft,
                repo_dir=repo_dir,
                reviewer_comments=reviewer_comments,
                memory=memory_text,
                epic_context=epic_ctx,
                extra_roots=extra_roots,
                message_history=resume_history,
                board_id=memory_board_id,
                language_instructions=language_instructions,
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            # ModelHTTPError subclasses RuntimeError, so a transient model
            # blip (OpenRouter 5xx/429/timeout, DeepSeek reasoning-400) is
            # caught here too — re-raise it so the worker stage-retries a
            # fresh refine run instead of a hard BLOCK. Fatal RuntimeErrors
            # (missing API key) fall through and block as before.
            from ..runtime.transient_errors import reraise_if_transient

            reraise_if_transient(e)
            return Outcome(State.BLOCKED, str(e))

        # --- pause detection ---
        # check_for_pause looks at THIS run's new messages so an old
        # ask_user sentinel from a prior turn (still in the saved
        # transcript on resume) doesn't re-trigger. The full transcript
        # (``conversation_state``) is still what gets persisted for
        # resume.
        if check_for_pause(result.new_messages):
            save_conversation_state(ws, result.conversation_state, "refine")
            ctx.service.transition(
                ticket.id,
                State.AWAITING_USER_REPLY,
                note="paused — agent asked a clarifying question",
            )
            log.info(
                "%s: paused refine — agent invoked ask_user",
                ticket.id,
            )
            return Outcome(State.AWAITING_USER_REPLY)

        # Refine produced a normal output (no pause) — clear any stale
        # saved state from earlier pause/resume cycles so it cannot leak
        # into downstream stages as a phantom resume context.
        clear_conversation_state(ws, "refine")

        if result.updated_memory:
            persist_memory(refine_memory_path, result.updated_memory)

        if result.title and result.title.strip():
            ctx.service.set_title(ticket.id, result.title.strip())

        # --- epic body handling (non-split path) ---
        # In autonomous mode: apply immediately to the epic.
        # In gated mode: store as artifact in child workspace for
        # later application on approval.
        if result.epic_body and result.epic_body.strip() and epic_ctx:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == "epic":
                if not ctx.settings.require_approval:
                    new_hash = ctx.service.workspace(parent).write_description(
                        result.epic_body.strip()
                    )
                    ctx.service.set_content_hash(parent.id, new_hash)
                else:
                    (ws.artifacts_dir / "epic-body-proposed.md").write_text(
                        result.epic_body.strip(), encoding="utf-8"
                    )

        # --- preserve the raw draft (always, for traceability) ---
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )

        # --- write file map artifact ---
        if result.file_map:
            (ws.artifacts_dir / "file_map.json").write_text(
                json.dumps(
                    [{"file": e.file, "note": e.note} for e in result.file_map],
                    indent=2,
                ),
                encoding="utf-8",
            )

        # --- write reference_files artifact ---
        if result.reference_files:
            ref_path = ws.artifacts_dir / "reference_files.json"
            ref_path.write_text(
                json.dumps(
                    [{"path": p} for p in result.reference_files],
                    indent=2,
                ),
                encoding="utf-8",
            )

        # --- no-change-needed path ---
        # When refine concludes the spec is informational — full
        # investigation already in the body, acceptance criteria are
        # "post a comment explaining why no change is needed", or a
        # parallel ticket already shipped the fix — it returns
        # no_change_needed=true. The stage files the rationale as a
        # top-level comment on the ticket and transitions
        # DRAFT → DONE, skipping implement / review / document /
        # deliver / merge. This is the bypass that catches the
        # d129-style "implement gets stuck because there's nothing
        # to write" failure mode.
        if result.no_change_needed and not result.split and not result.promote_to_epic:
            rationale = (result.no_change_rationale or "").strip()
            if not rationale:
                # Degrade to single-spec; the operator can see the
                # spec and decide. Don't transition to DONE on an
                # empty rationale — that would close the ticket with
                # no explanation, which is worse than a normal
                # approval.
                log.warning(
                    "%s: no_change_needed but no rationale — "
                    "degrading to normal single-spec path",
                    ticket.id,
                )
            else:
                # If this ticket was previously implemented (has a
                # branch), verify the implementation is actually
                # merged to the base branch before closing as DONE.
                # Otherwise the work lives only on an orphaned
                # branch and will be lost when the ticket closes.
                if ticket.branch and not _verify_branch_merged(repo_dir, ticket):
                    return Outcome(
                        State.BLOCKED,
                        f"{UNMERGED_BRANCH_PREFIX} '{ticket.branch}' "
                        "but is not merged to main. "
                        "Merge the PR or manually close.",
                    )

                # The rationale is the agent's conclusion — into
                # history (note), not comments. Truncate to keep the
                # event row scannable; the full rationale lives in
                # the refine artifact (draft-original.md captures
                # spec-shape context too).
                short = rationale[:400] + ("…" if len(rationale) > 400 else "")
                return Outcome(
                    State.DONE,
                    f"no change needed — {short}",
                )

        # --- promote-to-epic path ---
        # When refine decides the spec is too varied for one pass
        # (manifest-driven, ≥6 children, per-item deep specs needed),
        # it returns promote_to_epic=True. The stage converts the
        # ticket to an epic, writes the strategic epic_body to the
        # workspace description, and synchronously invokes
        # epic-breakdown to spawn the children. After that the epic
        # sits in EPIC_OPEN — its children flow through refine
        # individually on their own cycles.
        if result.promote_to_epic and not result.split:
            from ..agents.epic_breakdown import run_epic_breakdown_agent

            epic_body = (result.epic_body or result.spec_markdown or "").strip()
            if not epic_body:
                log.warning(
                    "%s: promote_to_epic but no epic_body — "
                    "falling back to original draft",
                    ticket.id,
                )
                epic_body = draft or ticket.title
            new_hash = ws.write_description(epic_body)
            ctx.service.set_content_hash(ticket.id, new_hash)
            ctx.service.promote_to_epic(ticket.id)
            try:
                breakdown = run_epic_breakdown_agent(
                    settings=s,
                    epic_title=ticket.title,
                    epic_description=epic_body,
                )
                # Advisory pre-filing dedup: flag (never drop) children
                # whose scope overlaps a recent ticket or an earlier
                # sibling in this batch. Best-effort — a failure here must
                # not block filing.
                from ..dedup import annotate_child_body, find_child_overlaps

                child_titles = list(breakdown.child_titles)
                child_bodies = list(breakdown.child_bodies)
                overlap_notes = find_child_overlaps(
                    ctx.service,
                    ticket.id,
                    child_titles,
                    child_bodies,
                    s,
                    datetime.now(timezone.utc),
                )
                created_ids: list[str] = []
                for child_title, child_body, dup_note in zip(
                    child_titles,
                    child_bodies,
                    overlap_notes,
                ):
                    if dup_note:
                        log.warning(
                            "epic %s: child '%s' flagged as possible duplicate — %s",
                            ticket.id,
                            child_title,
                            dup_note,
                        )
                        child_body = annotate_child_body(child_body, dup_note)
                    child = ctx.service.create(
                        title=child_title,
                        description=child_body,
                        kind="task",
                        parent_id=ticket.id,
                    )
                    created_ids.append(child.id)
                # Linear dependency chain (C0 → C1 → C2 → …) — matches
                # the /generate-children route's default behaviour.
                for i in range(1, len(created_ids)):
                    ctx.service.set_depends_on(
                        created_ids[i],
                        [created_ids[i - 1]],
                    )
                # Apply the breakdown's revised epic body if any.
                if breakdown.epic_body and breakdown.epic_body.strip():
                    revised_hash = ws.write_description(
                        breakdown.epic_body.strip(),
                    )
                    ctx.service.set_content_hash(ticket.id, revised_hash)
                note = f"promoted to epic; spawned {len(created_ids)} child(ren)"
            except Exception:
                log.exception(
                    "%s: epic-breakdown after promote_to_epic failed — "
                    "epic body is in place, children left for "
                    "/generate-children",
                    ticket.id,
                )
                note = (
                    "promoted to epic; breakdown failed — "
                    "use /generate-children to retry"
                )
            return Outcome(State.EPIC_OPEN, note)

        # --- normal single-scope path ---
        if not result.split:
            spec = result.spec_markdown or ""
            if _spec_is_degenerate(spec):
                log.warning(
                    "%s: refiner produced no usable spec (empty or "
                    "placeholder %r) — proceeding with original draft",
                    ticket.id,
                    spec[:60],
                )
                next_state, _auto_reason = _resolve_next_state(
                    ctx, "", ticket.id, source=ticket.source
                )
                return Outcome(
                    next_state, "refined (no usable spec — kept original draft)"
                )

            # --- spec review (conciseness pass) ---
            if s.spec_review_enabled and not reviewer_comments:
                try:
                    review_result = refining.review_spec_for_conciseness(
                        settings=s,
                        spec_markdown=spec,
                    )
                    (ws.artifacts_dir / "refine-verbose.md").write_text(
                        spec,
                        encoding="utf-8",
                    )
                    concise = review_result.concise_spec
                    if _spec_is_degenerate(concise):
                        log.warning(
                            "%s: spec review returned empty/placeholder "
                            "concise spec, using verbose spec",
                            ticket.id,
                        )
                    else:
                        spec = concise
                        log.info(
                            "%s: spec review: %s",
                            ticket.id,
                            review_result.stripped_summary,
                        )
                except Exception:
                    log.warning(
                        "%s: spec review failed, using verbose spec",
                        ticket.id,
                        exc_info=True,
                    )

            new_hash = ws.write_description(spec)
            ctx.service.set_content_hash(ticket.id, new_hash)

            # --- post-agent thread acknowledgment ---
            if reviewer_comments and open_thread_ids:
                acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)

            next_state, auto_note = _resolve_next_state(
                ctx, spec, ticket.id, source=ticket.source
            )
            note = "refined"
            if auto_note:
                note += f" | {auto_note}"
            return Outcome(next_state, note)

        # --- multi-scope split path ---
        children_raw = result.children
        if not children_raw or len(children_raw) == 0:
            # Degrade gracefully: treat as single-spec with whatever we got.
            spec = result.spec_markdown or ""
            if _spec_is_degenerate(spec):
                log.warning(
                    "%s: refiner produced no usable spec "
                    "(split with no children) — "
                    "proceeding with original draft",
                    ticket.id,
                )
                next_state, _auto_reason = _resolve_next_state(
                    ctx, "", ticket.id, source=ticket.source
                )
                # --- post-agent thread acknowledgment ---
                if reviewer_comments and open_thread_ids:
                    acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)
                return Outcome(
                    next_state,
                    "refined (empty spec, split degraded — kept original draft)",
                )
            new_hash = ws.write_description(spec)
            ctx.service.set_content_hash(ticket.id, new_hash)

            # --- post-agent thread acknowledgment ---
            if reviewer_comments and open_thread_ids:
                acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)

            next_state, auto_note = _resolve_next_state(
                ctx, spec, ticket.id, source=ticket.source
            )
            note = "refined (split degraded — no valid children)"
            if auto_note:
                note += f" | {auto_note}"
            return Outcome(next_state, note)

        # Validate and collect valid children.
        valid_children: list[dict] = []
        for child in children_raw:
            child_title = (child.title or "").strip()
            spec_md = (child.spec_markdown or "").strip()
            if not child_title or not spec_md:
                continue
            deps = child.depends_on or []
            if not isinstance(deps, list):
                deps = []
            # Keep only non-negative integer indices.
            deps = [d for d in deps if isinstance(d, int) and d >= 0]
            valid_children.append(
                {
                    "title": child_title,
                    "spec_markdown": spec_md,
                    "depends_on": deps,
                }
            )

        if len(valid_children) == 0:
            # --- post-agent thread acknowledgment ---
            if reviewer_comments and open_thread_ids:
                acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)
            return Outcome(State.BLOCKED, "refiner produced no valid split children")

        # --- spec review for split children (conciseness pass) ---
        if s.spec_review_enabled and not reviewer_comments:
            for i, child in enumerate(valid_children):
                try:
                    review_result = refining.review_spec_for_conciseness(
                        settings=s,
                        spec_markdown=child["spec_markdown"],
                    )
                    (ws.artifacts_dir / f"refine-verbose-child-{i + 1}.md").write_text(
                        child["spec_markdown"],
                        encoding="utf-8",
                    )
                    concise = review_result.concise_spec
                    if _spec_is_degenerate(concise):
                        log.warning(
                            "%s: spec review child %d returned empty/placeholder "
                            "concise spec, using verbose spec",
                            ticket.id,
                            i + 1,
                        )
                    else:
                        child["spec_markdown"] = concise
                        log.info(
                            "%s: spec review child %d: %s",
                            ticket.id,
                            i + 1,
                            review_result.stripped_summary,
                        )
                except Exception:
                    log.warning(
                        "%s: spec review failed for child %d, using verbose spec",
                        ticket.id,
                        i + 1,
                        exc_info=True,
                    )

        if len(valid_children) == 1:
            # Only one valid child — fall back to single-spec path.
            child = valid_children[0]
            new_hash = ws.write_description(child["spec_markdown"])
            ctx.service.set_content_hash(ticket.id, new_hash)
            # Update the ticket title: agent's explicit title beats
            # the child's title (which is a fallback).
            if not (result.title and result.title.strip()):
                ctx.service.set_title(ticket.id, child["title"])

            # --- post-agent thread acknowledgment ---
            if reviewer_comments and open_thread_ids:
                acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)

            next_state, auto_note = _resolve_next_state(
                ctx, child["spec_markdown"], ticket.id, source=ticket.source
            )
            note = "refined (single child, no split)"
            if auto_note:
                note += f" | {auto_note}"
            return Outcome(next_state, note)

        # Create child tickets.
        child_ids: list[str] = []
        for i, child in enumerate(valid_children):
            child_ticket = ctx.service.create(
                title=child["title"],
                description=child["spec_markdown"],
                source=ticket.source,
            )
            child_ids.append(child_ticket.id)

        # Reparent children: if the ticket already belongs to an
        # epic, reparent to that epic; otherwise create a new
        # umbrella epic so children appear under a visible grouping
        # entity rather than a closed parent.
        existing_epic_id: str | None = None
        if ticket.parent_id is not None:
            parent_candidate = ctx.service.get(ticket.parent_id)
            if parent_candidate is not None and parent_candidate.kind == "epic":
                existing_epic_id = ticket.parent_id
                for cid in child_ids:
                    ctx.service.set_parent(cid, existing_epic_id)
        if existing_epic_id is None:
            epic_title = (result.title and result.title.strip()) or ticket.title.strip()
            epic_desc = (result.spec_markdown and result.spec_markdown.strip()) or draft
            epic = ctx.service.create(
                title=epic_title,
                description=epic_desc,
                kind="epic",
                source=ticket.source,
            )
            for cid in child_ids:
                ctx.service.set_parent(cid, epic.id)

        # Resolve depends_on indices → real ticket IDs.
        for i, child in enumerate(valid_children):
            if child["depends_on"]:
                resolved = []
                for idx in child["depends_on"]:
                    if 0 <= idx < i and idx < len(child_ids):
                        resolved.append(child_ids[idx])
                if resolved:
                    ctx.service.set_depends_on(child_ids[i], resolved)

        # Transition each child to HUMAN_ISSUE_APPROVAL or READY.
        for i, cid in enumerate(child_ids):
            child_state, auto_note = _resolve_next_state(
                ctx,
                valid_children[i]["spec_markdown"],
                cid,
            )
            child_note = f"split from {ticket.id}"
            if auto_note:
                child_note += f" | {auto_note}"
            ctx.service.transition(cid, child_state, note=child_note)

        # Apply epic body immediately in split path regardless of
        # require_approval — the children each go through their own
        # approval flow, and the original ticket is closed so there
        # is no single approval event to gate on.
        if result.epic_body and result.epic_body.strip() and epic_ctx:
            parent = ctx.service.get(ticket.parent_id)
            if parent is not None and parent.kind == "epic":
                new_hash = ctx.service.workspace(parent).write_description(
                    result.epic_body.strip()
                )
                ctx.service.set_content_hash(parent.id, new_hash)

        # Close the original ticket.
        ids_note = ", ".join(child_ids)

        # --- post-agent thread acknowledgment ---
        if reviewer_comments and open_thread_ids:
            acknowledge_unanswered_threads(ctx, ticket, open_thread_ids)

        return Outcome(
            State.CLOSED,
            f"split into {ids_note}",
        )
