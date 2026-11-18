"""Dedup and in-flight advisory gate checks for the refine stage.

Extracted from :mod:`.gates` to keep that module focused.  These are the
cheap, best-effort dedup guards that run before the expensive refine agent:
the dedup / already-done guard, dedup-target validation, the concurrent
in-flight advisory, and the advisory-resolution gate.

They are plain module-level functions; :class:`.gates.RefineGatesMixin`
re-imports them and re-exposes them as staticmethods so the mixin API
(and ``RefineStage``) is unchanged.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ...agents import dedup
from ...core.datetime_utils import _as_utc
from ...core.dedup import _extract_paths, _scope_paths
from ...core.models import SourceKind, Ticket, TicketKind
from ...core.states import State
from ...scm.vcs import git_ops
from ...stages.ci_fix_helpers import UPSTREAM_CI_BLOCK_MARKER
from ...stages.ci_infra_block import REPO_SETTING_BLOCK_MARKER
from ..base import Outcome, StageContext
from .helpers import (
    DEDUP_ALREADY_DONE_PREFIX,
    DEDUP_DUPLICATE_PREFIX,
    MARK_DONE_NOTE_PREFIX,
    NON_IMPLEMENTATION_CLOSE_PREFIXES,
    OPERATOR_SENDBACK_PREFIX,
    REFINE_PROGRESS_STATES,
    _advisory_candidate_id,
    _build_candidates_block,
    _rationale_claims_external_fix,
    _strip_advisory_block,
    log,
    verify_claim,
)


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
    except Exception:
        log.debug("%s: dedup sendback-check skipped", ticket.id, exc_info=True)

    # Gather candidate tickets: all non-terminal + recently closed.
    all_tickets = ctx.service.list()
    now = datetime.now(UTC)
    lookback_cutoff = datetime.fromtimestamp(
        now.timestamp() - s.dedup_lookback_days * 86400, tz=UTC
    )
    non_terminal = {State.CLOSED, State.ERRORED}
    candidates = [
        t
        for t in all_tickets
        if t.id != ticket.id
        and (
            t.state not in non_terminal
            or (t.state == State.CLOSED and _as_utc(t.updated_at) >= lookback_cutoff)
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
        t for t in candidates if t.kind != TicketKind.EPIC or t.id == ticket.parent_id
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
            "%s: no candidate token overlap (%d candidates) — skipping dedup LLM call",
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
        if _is_valid_dedup_target(ctx, ticket, dup_id, repo_dir, draft=draft):
            outcome = _dedup_close_protocol(
                ctx,
                ticket,
                dup_id,
                verdict.get("reason") or "no reason",
                draft,
            )
            if outcome is not None:
                return outcome
            # The protocol kept the incoming ticket open as the
            # actionable owner (candidate could not be made actionable) —
            # proceed with refine below.
            return None
        log.info(
            "%s: dedup verdict named duplicate_of=%s but it is not a "
            "valid dedup target (terminal/declined/circular/unmerged) — "
            "proceeding with refine",
            ticket.id,
            dup_id,
        )
    done_id = verdict.get("already_done")
    if done_id:
        # Verify the already_done claim against the draft's target
        # files before accepting the verdict.  A claim that cites a
        # PR/commit that does NOT touch any file named in the draft
        # is a provably false dismissal — proceed with refine instead.
        reason_text = verdict.get("reason", "")
        draft_paths = _extract_paths(draft)
        if (
            draft_paths
            and reason_text
            and not verify_claim(reason_text, draft_paths, repo_dir)
        ):
            log.info(
                "%s: dedup already_done claim (%s) could not be "
                "verified against target files (%s) — "
                "proceeding with refine instead of short-circuiting",
                ticket.id,
                reason_text[:120],
                ", ".join(draft_paths[:5]),
            )
        elif _is_valid_dedup_target(ctx, ticket, done_id, repo_dir, draft=draft):
            return Outcome(
                State.DONE,
                f"{DEDUP_ALREADY_DONE_PREFIX}{done_id}: {verdict.get('reason', 'no reason')}",
            )
        else:
            log.info(
                "%s: dedup verdict named already_done=%s but it is not a "
                "valid dedup target (terminal/declined/circular/unmerged) — "
                "proceeding with refine",
                ticket.id,
                done_id,
            )
    return None


def _is_valid_dedup_target(
    ctx: StageContext,
    ticket: Ticket,
    candidate_id: str,
    repo_dir: Path | None,
    draft: str | None = None,
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
    - an un-refined ``DRAFT`` candidate whose history never reached a
      refine-progress state (closing a further-along ticket into it
      risks burying the fix in a ticket that may never be implemented);
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
            # Not a ticket id (e.g. a commit hash) — verify
            # the commit is an ancestor of origin/main when possible.
            if repo_dir is None:
                return True
            # Refresh origin/main before the ancestry assertion so a stale
            # clone cannot reject a commit that is actually merged upstream.
            git_ops.refresh_origin_target(repo_dir, "main")
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_dir),
                        "merge-base",
                        "--is-ancestor",
                        candidate_id,
                        "origin/main",
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    return True
                if result.returncode == 1:
                    log.info(
                        "%s: dedup target '%s' is not an ancestor of "
                        "origin/main — rejecting as dedup target",
                        ticket.id,
                        candidate_id,
                    )
                    return False
                # Any other exit code — best-effort allow.
                log.debug(
                    "%s: git merge-base check for '%s' exited %d — "
                    "allowing (best-effort)",
                    ticket.id,
                    candidate_id,
                    result.returncode,
                )
                return True
            except Exception:
                log.debug(
                    "%s: git merge-base check for '%s' failed — allowing (best-effort)",
                    ticket.id,
                    candidate_id,
                    exc_info=True,
                )
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

        # Un-refined DRAFT candidate: closing a further-along ticket into a
        # candidate that has never progressed past DRAFT risks burying the
        # fix in a ticket that may never be implemented.  Prefer keeping the
        # current ticket, which is actively being refined.
        if cand.state == State.DRAFT and not any(
            ev.state in REFINE_PROGRESS_STATES for ev in history
        ):
            log.info(
                "%s: dedup candidate %s is an un-refined DRAFT (no refine "
                "progress) — not a valid dedup target, proceeding with refine",
                ticket.id,
                candidate_id,
            )
            return False

        # Sibling-with-same-parent bypass: if the candidate shares the
        # same parent as the current ticket, it's the same piece of work
        # regardless of branch status — allow the dedup.  This handles
        # the parallel-consumer-migration case where two sibling tickets
        # describe identical work under the same parent epic but one
        # hasn't had its branch merged yet.
        if ticket.parent_id is not None and cand.parent_id == ticket.parent_id:
            return True

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

        # Human-closed-with-claim guard: if the candidate reached DONE via
        # a human mark_done (no branch) and the close note claims an
        # external fix (PR/commit reference), the claim is unverified —
        # reject the dedup target so refine proceeds and produces a spec
        # that goes through implement for live re-check against HEAD.
        #
        # A bare operator ``mark done`` is also rejected: a mark-done'd
        # ticket was never implemented (no PR merged), so closing a live
        # ticket as "duplicate of" it would annihilate the capability —
        # the same supersede/dedup failure class as the external-fix guard
        # (mill #3158).  This matters most for orphaned-PR tracking tickets,
        # which an operator mark-done's without the tracked PR ever merging.
        if not cand.branch:
            for ev in history:  # type: ignore[attr-defined]
                if ev.state == State.DONE:
                    note = (ev.note or "").strip().lower()
                    if note.startswith(MARK_DONE_NOTE_PREFIX):
                        log.info(
                            "%s: dedup candidate %s was operator mark-done'd "
                            "(no implementation) — not a valid dedup target, "
                            "proceeding with refine",
                            ticket.id,
                            candidate_id,
                        )
                        return False
                    if _rationale_claims_external_fix(note):
                        log.info(
                            "%s: dedup candidate %s was human-closed with an "
                            "external-fix claim — not a valid dedup target "
                            "(unverified), proceeding with refine",
                            ticket.id,
                            candidate_id,
                        )
                        return False

        # The candidate reached DONE via a real implementation, but if
        # that implementation lives only on an unmerged branch the work
        # never reached main — "already implemented in X" is invalid.
        # Resolve through the package façade so a test that patches
        # ``robotsix_mill.stages.refine._verify_branch_merged`` still
        # takes effect.
        from robotsix_mill.stages import refine as _facade

        if cand.branch and not _facade._verify_branch_merged(repo_dir, cand):
            log.info(
                "%s: dedup candidate %s has branch '%s' not merged to "
                "main — not a valid dedup target",
                ticket.id,
                candidate_id,
                cand.branch,
            )
            return False

        # File-map overlap check: when a draft is supplied, verify
        # that the candidate's declared scope paths overlap with
        # paths extracted from the current draft.  Best-effort:
        # any extraction/read failure degrades to "allow".
        if draft is not None:
            try:
                draft_paths = _extract_paths(draft)
                if draft_paths:
                    try:
                        body = ctx.service.workspace(cand).read_description()
                    except Exception:
                        body = ""
                    if body:
                        scope = _scope_paths(body)
                        if scope and not (set(draft_paths) & scope):
                            log.info(
                                "%s: dedup target %s scope paths have "
                                "no overlap with current draft paths — "
                                "rejecting as dedup target",
                                ticket.id,
                                candidate_id,
                            )
                            return False
            except Exception:
                log.debug(
                    "%s: file-map overlap check failed for %s — allowing (best-effort)",
                    ticket.id,
                    candidate_id,
                    exc_info=True,
                )
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


# ---------------------------------------------------------------------------
# Deterministic dedup-close protocol
# ---------------------------------------------------------------------------
#
# A plain dedup close ("duplicate of <id>") is only valid when the
# surviving candidate is both actionable and covers the incoming draft.
# Closing into a candidate that is blocked / awaiting a human / parked at
# an approval gate — or whose spec lacks requirements the draft carries —
# is a force-close of new scope, the same lost-work shape as the operator
# mark-done force-close but reached implicitly through the dedup path
# (observed on robotsix-browser: 20261118T173536Z was closed as a
# duplicate of a repo-setting-blocked candidate, orphaning the 80%
# coverage-floor requirement with zero tickets left tracking it).  So
# instead of a bare close, port the draft's delta into the candidate's
# description, re-queue the candidate when it is blocked, and only then
# close the incoming ticket pointing at the updated candidate.  When the
# candidate still cannot be made actionable, keep the incoming ticket
# open as the actionable owner and mark the older one superseded.

# Open states with no pipeline-driven implementation path until a human
# acts: the worker never runs them, so a dedup close onto one buries the
# draft's scope in a ticket that will not move.
NON_ACTIONABLE_DEDUP_STATES: frozenset[State] = frozenset(
    {
        State.BLOCKED,
        State.AWAITING_USER_REPLY,
        State.HUMAN_ISSUE_APPROVAL,
    }
)

# ``BLOCKED``-note markers that no re-queue resolves — the block is an
# operator/settings/external dependency ("repo setting required:" needs a
# human to flip a GitHub setting; "Upstream CI breakage detected" is owned
# by the upstream-CI recovery runner).  Mirrors the hard excludes in
# ``agents/runners/blocked_auto_resume_runner.py``: a candidate the
# pipeline itself would never auto-resume must not be silently force-merged
# either.
NON_RESUMABLE_BLOCK_MARKERS: tuple[str, ...] = (
    REPO_SETTING_BLOCK_MARKER,
    UPSTREAM_CI_BLOCK_MARKER,
)

# Author tag stamped on candidate spec updates made by this protocol.
_DEDUP_PROTOCOL_AUTHOR = "refine-dedup"


def _blocked_candidate_is_resumable(ctx: StageContext, cand: Ticket) -> bool:
    """Return whether a BLOCKED candidate can be re-queued by the pipeline.

    False when the latest BLOCKED history note carries a
    :data:`NON_RESUMABLE_BLOCK_MARKERS` marker (an operator/external
    dependency the pipeline cannot satisfy).  True otherwise — the generic
    resumable blocks ("agent error", stage timeouts) un-block fine.
    """
    # Same partial-`list?[TicketEvent]` quirk as the other history
    # iterations in this module (baselined in mypy-baseline.txt).
    for ev in ctx.service.history(cand.id, order="desc"):  # type: ignore[attr-defined]
        if ev.state is State.BLOCKED:
            note = (ev.note or "")[:400].casefold()
            return not any(
                marker.casefold() in note for marker in NON_RESUMABLE_BLOCK_MARKERS
            )
    # No BLOCKED event found — let resume_blocked decide (it raises when
    # the ticket cannot actually be resumed, and the caller falls back to
    # the supersede branch).
    return True


def _draft_carries_absent_requirement(
    ctx: StageContext, cand: Ticket, draft: str
) -> bool:
    """Return whether the incoming *draft* names requirements the
    candidate's declared spec does not.

    Deterministic proxy for "the draft carries requirements absent from
    the candidate's spec": every backtick path token in the draft that is
    not covered by the candidate's Scope / Acceptance / file_map sections
    is a requirement the candidate would not implement.  A candidate with
    no declared scope cannot be proven to cover anything, so any draft
    path counts as absent.  Best-effort: empty drafts and extraction
    failures return False.
    """
    try:
        draft_paths = set(_extract_paths(draft or ""))
        if not draft_paths:
            return False
        body = ctx.service.workspace(cand).read_description() or ""
        scope = _scope_paths(body)
        if not scope:
            return True
        return bool(draft_paths - scope)
    except Exception:
        log.debug(
            "dedup protocol: absent-requirement check failed for %s — "
            "assuming no delta",
            cand.id,
            exc_info=True,
        )
        return False


def _port_delta_into_candidate(
    ctx: StageContext, ticket: Ticket, cand: Ticket, draft: str, reason: str
) -> None:
    """Append the incoming draft's delta to the candidate's description
    with a provenance note (a dedup-merge port, not a plain close).
    """
    try:
        body = ctx.service.workspace(cand).read_description() or ""
    except Exception:
        body = ""
    delta_block = (
        f"\n\n## Dedup-merged delta — from {ticket.id}\n\n"
        f"The refine dedup protocol merged this incoming draft into this "
        f"ticket instead of closing it as a plain duplicate ({reason}).\n\n"
        f"Provenance: incoming ticket {ticket.id} ({ticket.title}). "
        f"Original description:\n\n"
        f"{draft}\n"
    )
    ctx.service.update_description(
        cand.id, body.rstrip() + delta_block, author=_DEDUP_PROTOCOL_AUTHOR
    )


def _mark_candidate_superseded(
    ctx: StageContext, ticket: Ticket, cand: Ticket, reason: str
) -> None:
    """Mark the older *cand* superseded by the incoming *ticket*.

    Leaves the candidate open (a human may still resolve its block) but
    records the substitution in both the description (visible on the
    board) and the history trail.
    """
    banner = (
        f"\n\n## Superseded — kept open by {ticket.id}\n\n"
        f"The refine dedup protocol kept the newer incoming ticket "
        f"{ticket.id} ({ticket.title}) open as the actionable owner "
        f"because this ticket ({cand.state.value}) could not be made "
        f"actionable; do not implement this ticket in isolation "
        f"({reason})."
    )
    try:
        body = ctx.service.workspace(cand).read_description() or ""
    except Exception:
        body = ""
    try:
        ctx.service.update_description(
            cand.id, body.rstrip() + banner, author=_DEDUP_PROTOCOL_AUTHOR
        )
    except Exception:
        log.debug(
            "%s: could not banner candidate %s as superseded",
            ticket.id,
            cand.id,
            exc_info=True,
        )
    ctx.service.add_history_note(
        cand.id,
        f"superseded by {ticket.id} ({ticket.title}): refine dedup kept the "
        f"newer ticket open as actionable owner — {reason}",
    )


def _dedup_close_protocol(
    ctx: StageContext,
    ticket: Ticket,
    cand_id: str,
    reason: str,
    draft: str,
) -> Outcome | None:
    """Resolve a duplicate-of verdict through the deterministic
    dedup-close protocol.

    Returns the close :class:`Outcome` when the incoming ticket should be
    closed as a duplicate of *cand_id* — a plain close for a healthy
    candidate that covers the draft; a delta-port (+ re-queue when
    blocked) close otherwise.  Returns ``None`` when the candidate cannot
    be made actionable: the incoming ticket stays open as the actionable
    owner and the older candidate is marked superseded.
    """
    cand = ctx.service.get(cand_id)
    if cand is None:
        # Unresolvable target (e.g. a commit hash) — cannot evaluate the
        # protocol; keep the plain close behavior.
        return Outcome(
            State.DONE,
            f"{DEDUP_DUPLICATE_PREFIX}{cand_id}: {reason}",
        )

    not_actionable = cand.state in NON_ACTIONABLE_DEDUP_STATES
    carries_delta = _draft_carries_absent_requirement(ctx, cand, draft)
    if not not_actionable and not carries_delta:
        # Candidate is actionable and covers the draft — plain close.
        return Outcome(
            State.DONE,
            f"{DEDUP_DUPLICATE_PREFIX}{cand_id}: {reason}",
        )

    can_become_actionable = cand.state not in NON_ACTIONABLE_DEDUP_STATES or (
        cand.state is State.BLOCKED and _blocked_candidate_is_resumable(ctx, cand)
    )

    if can_become_actionable:
        try:
            _port_delta_into_candidate(ctx, ticket, cand, draft, reason)
            if cand.state is State.BLOCKED:
                ctx.service.resume_blocked(
                    cand_id,
                    note=(
                        f"[dedup-refine] re-queued after merging delta from "
                        f"{ticket.id}: {reason}"
                    ),
                )
        except Exception:
            log.warning(
                "%s: dedup protocol could not make candidate %s actionable "
                "(delta port / re-queue failed) — keeping %s open and "
                "marking the candidate superseded",
                ticket.id,
                cand_id,
                ticket.id,
                exc_info=True,
            )
            _mark_candidate_superseded(ctx, ticket, cand, reason)
            return None
        log.info(
            "%s: dedup duplicate of %s — delta merged into candidate's "
            "description and candidate re-queued; closing as duplicate of "
            "the updated candidate",
            ticket.id,
            cand_id,
        )
        return Outcome(
            State.DONE,
            f"{DEDUP_DUPLICATE_PREFIX}{cand_id}: {reason} — delta merged into candidate",
        )

    # The candidate cannot be made actionable (awaiting a human reply /
    # parked at an approval gate, or hard-blocked).  Keep the incoming
    # ticket open as the actionable owner and mark the older candidate
    # superseded.
    _mark_candidate_superseded(ctx, ticket, cand, reason)
    return None


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
    if ticket.parent_id is not None or ticket.kind == TicketKind.EPIC:
        return draft
    if len(draft) < 100:
        return draft

    from ...core.dedup import annotate_child_body, find_inflight_overlap

    dedup_labels: list[str] | None = None
    if ticket.source == SourceKind.CI:
        from ...core.dedup import _ci_draft_fingerprint

        # Extract workflow file path from the **Path:** metadata line.
        _wf_path = ""
        for line in draft.splitlines():
            if line.strip().startswith("**Path:**"):
                _wf_path = line.strip()[len("**Path:**") :].strip()
                break

        fp = _ci_draft_fingerprint(draft, path=_wf_path)
        label = f"ci_fp:{fp}"
        dedup_labels = [label]
        # Store fingerprint label on THIS ticket for future dedup checks.
        # Re-fetch from DB to avoid stale in-memory labels.
        current = ctx.service.get(ticket.id)
        existing_labels: list[str] = []
        if current is not None and current.labels:
            try:
                existing_labels = json.loads(current.labels)
                if not isinstance(existing_labels, list):
                    existing_labels = []
            except json.JSONDecodeError, TypeError:
                pass
        if label not in existing_labels:
            ctx.service.set_labels(ticket.id, [*existing_labels, label])

    note = find_inflight_overlap(
        ctx.service,
        ticket.id,
        ticket.title,
        draft,
        s,
        datetime.now(UTC),
        dedup_labels=dedup_labels,
    )
    if not note:
        return draft

    log.warning(
        "%s: draft flagged as possible duplicate of a concurrent in-flight ticket — %s",
        ticket.id,
        note,
    )
    annotated = annotate_child_body(
        draft, note, source_desc="draft-intake pre-refine dedup"
    )
    new_hash = ws.write_description(annotated)
    ctx.service.set_content_hash(ticket.id, new_hash)
    return annotated


def _verify_advisory_dedup(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    repo_dir: Path | None,
    ws,
    s,
) -> Outcome | str:
    """Cheap dedup-verification gate that resolves a carried advisory.

    Runs after ``_run_inflight_advisory`` and before the expensive
    refine agent.  Resolves the ``Possible duplicate of <id>`` advisory
    (if any) with a single cheapest-tier ``run_dedup_check`` against
    ONLY the named candidate — never the full candidate set.

    1. No advisory → return *draft* unchanged (no LLM call).
    2. Candidate cannot be resolved → strip advisory, return cleaned.
    3. Cheap check returns ``duplicate_of`` / ``already_done`` AND
       ``_is_valid_dedup_target`` passes → return ``Outcome(DONE, …)``.
    4. Otherwise (not a duplicate / invalid target) → strip advisory,
       persist the cleaned body, and return it so refine proceeds.

    Entirely best-effort — any exception logs and returns *draft*
    unchanged.
    """
    try:
        if not s.refine_advisory_dedup_enabled:
            return draft

        cand_id = _advisory_candidate_id(draft)
        if cand_id is None:
            return draft  # no advisory — no-op, no LLM call

        resolved = ctx.service.get(cand_id)
        if resolved is None:
            # Candidate doesn't exist — clear the advisory and proceed.
            log.debug(
                "%s: advisory candidate %s not found — stripping advisory",
                ticket.id,
                cand_id,
            )
            cleaned = _strip_advisory_block(draft)
            new_hash = ws.write_description(cleaned)
            ctx.service.set_content_hash(ticket.id, new_hash)
            return cleaned

        # Run the cheap dedup check against the SINGLE named candidate.
        candidates_json = _build_candidates_block([resolved], ctx)
        try:
            verdict = dedup.run_dedup_check(
                settings=s,
                draft_title=ticket.title,
                draft_body=_strip_advisory_block(draft),
                candidates_json=candidates_json,
                repo_dir=repo_dir,
            )
        except Exception:
            log.warning(
                "%s: advisory dedup check failed for %s — "
                "stripping advisory and proceeding",
                ticket.id,
                cand_id,
                exc_info=True,
            )
            cleaned = _strip_advisory_block(draft)
            new_hash = ws.write_description(cleaned)
            ctx.service.set_content_hash(ticket.id, new_hash)
            return cleaned

        # Check duplicate_of verdict.
        dup_id = verdict.get("duplicate_of")
        if dup_id and _is_valid_dedup_target(
            ctx, ticket, dup_id, repo_dir, draft=_strip_advisory_block(draft)
        ):
            outcome = _dedup_close_protocol(
                ctx,
                ticket,
                dup_id,
                verdict.get("reason") or "no reason",
                _strip_advisory_block(draft),
            )
            if outcome is not None:
                return outcome
            # Protocol kept the incoming ticket open as the actionable
            # owner (candidate could not be made actionable) — fall through
            # to strip the advisory and proceed with refine.

        # Check already_done verdict.
        done_id = verdict.get("already_done")
        if done_id:
            reason_text = verdict.get("reason", "")
            cleaned_draft = _strip_advisory_block(draft)
            draft_paths = _extract_paths(cleaned_draft)
            if (
                draft_paths
                and reason_text
                and not verify_claim(reason_text, draft_paths, repo_dir)
            ):
                log.info(
                    "%s: advisory already_done claim (%s) could not "
                    "be verified against target files (%s) — "
                    "proceeding with refine",
                    ticket.id,
                    reason_text[:120],
                    ", ".join(draft_paths[:5]),
                )
            elif _is_valid_dedup_target(
                ctx, ticket, done_id, repo_dir, draft=cleaned_draft
            ):
                return Outcome(
                    State.DONE,
                    f"{DEDUP_ALREADY_DONE_PREFIX}{done_id}: {verdict.get('reason', 'no reason')}",
                )

        # Not a valid duplicate — clear the advisory so refine doesn't
        # re-litigate the dedup question.
        log.debug(
            "%s: advisory dedup verdict not confirmed — stripping advisory",
            ticket.id,
        )
        cleaned = _strip_advisory_block(draft)
        new_hash = ws.write_description(cleaned)
        ctx.service.set_content_hash(ticket.id, new_hash)
        return cleaned
    except Exception:
        log.warning(
            "%s: advisory dedup verification failed — proceeding with refine",
            ticket.id,
            exc_info=True,
        )
        return draft
