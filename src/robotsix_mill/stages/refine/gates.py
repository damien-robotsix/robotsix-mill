"""Pre-refine gate phases for the refine stage.

A mixin (:class:`RefineGatesMixin`) holding the cheap, deterministic-or-
single-LLM-call guards that run *before* the expensive refine agent:
dedup / already-done, in-flight advisory, freshness, and obsolescence.
These are mixed into :class:`RefineStage` (in ``core.py``); they call the
pure helpers from :mod:`.helpers` and the agent modules from
:mod:`...agents`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import subprocess
from pathlib import Path

from ...agents import dedup, freshness, obsolescence
from ...config import Settings
from ...core.datetime_utils import _as_utc
from ...core.draft_target import (
    referenced_local_deliverable_paths,
    referenced_mill_paths_absent,
    resolve_mill_service,
)
from ...core.models import SourceKind, Ticket, TicketKind
from ...core.states import State
from ...core.workspace import Workspace
from ..base import Outcome, StageContext
from ...core.dedup import _extract_paths, _scope_paths

from .helpers import (
    DEDUP_ALREADY_DONE_PREFIX,
    DEDUP_DUPLICATE_PREFIX,
    FRESHNESS_STALE_PREFIX,
    NON_IMPLEMENTATION_CLOSE_PREFIXES,
    OBSOLESCENCE_GAP_PREFIX,
    OPERATOR_SENDBACK_PREFIX,
    REFINE_MILL_CONSUMER_FOLLOWUP_PREFIX,
    REFINE_MILL_MISROUTE_PREFIX,
    REFINE_PROGRESS_STATES,
    _advisory_candidate_id,
    _build_candidates_block,
    _rationale_claims_external_fix,
    _strip_advisory_block,
    log,
    verify_claim,
)


class RefineGatesMixin:
    """Pre-refine gate staticmethods mixed into :class:`RefineStage`."""

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
            t
            for t in candidates
            if t.kind != TicketKind.EPIC or t.id == ticket.parent_id
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
            if RefineGatesMixin._is_valid_dedup_target(
                ctx, ticket, dup_id, repo_dir, draft=draft
            ):
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
            elif RefineGatesMixin._is_valid_dedup_target(
                ctx, ticket, done_id, repo_dir, draft=draft
            ):
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

    @staticmethod
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
                        "%s: git merge-base check for '%s' failed — "
                        "allowing (best-effort)",
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
            if not cand.branch:
                for ev in history:  # type: ignore[attr-defined]
                    if ev.state == State.DONE:
                        note = (ev.note or "").lower()
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
                        "%s: file-map overlap check failed for %s — "
                        "allowing (best-effort)",
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
                ctx.service.set_labels(ticket.id, existing_labels + [label])

        note = find_inflight_overlap(
            ctx.service,
            ticket.id,
            ticket.title,
            draft,
            s,
            datetime.now(timezone.utc),
            dedup_labels=dedup_labels,
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
            if dup_id and RefineGatesMixin._is_valid_dedup_target(
                ctx, ticket, dup_id, repo_dir, draft=_strip_advisory_block(draft)
            ):
                return Outcome(
                    State.DONE,
                    f"{DEDUP_DUPLICATE_PREFIX}{dup_id}: {verdict.get('reason', 'no reason')}",
                )

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
                elif RefineGatesMixin._is_valid_dedup_target(
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
        s: Settings,
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
    def _run_mill_misroute_gate(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        repo_dir: Path | None,
        s: Settings,
    ) -> Outcome | None:
        """Detect a draft naming mill-specific source paths absent from
        the current checkout and either redirect it to the mill
        maintenance board (when there is no local deliverable) or keep
        it on the current board and best-effort file a consumer
        follow-up ticket (when a primary deliverable lives on the
        current checkout).

        Runs before any LLM-invoking gate; purely deterministic
        (filesystem ``.exists()`` checks against the cloned working
        tree).  Returns ``Outcome(State.DONE, …)`` when a full redirect
        is confirmed, or ``None`` to proceed with refine.
        """
        if not s.refine_mill_misroute_gate_enabled:
            return None

        absent = referenced_mill_paths_absent(ticket.title, draft, repo_dir)
        if not absent:
            return None

        # Confidence threshold: when the repo-local codebase clearly
        # exists (has pyproject.toml, src/, or tests/), a single absent
        # mill-prefixed path is more likely a false positive (a
        # conceptual/spec-descriptive path, or a stray mention) than a
        # signal to redirect the entire ticket.  Require ≥2 absent
        # paths before redirecting.
        if repo_dir is not None and len(absent) == 1:
            _indicators = ("pyproject.toml", "src", "tests")
            if any((repo_dir / p).exists() for p in _indicators):
                log.info(
                    "%s: only 1 absent mill path (%s) — below "
                    "confidence threshold of 2 for a repo that clearly "
                    "exists; proceeding with refine on current board",
                    ticket.id,
                    absent[0],
                )
                return None

        # Check whether the ticket has a primary deliverable on the
        # current checkout — if so, keep it here and best-effort file a
        # consumer follow-up on the mill board instead of redirecting
        # the whole ticket.
        local = referenced_local_deliverable_paths(ticket.title, draft, repo_dir)
        if local:
            log.info(
                "%s: draft references absent mill paths (%s) but has a "
                "local deliverable on this checkout (%s) — keeping on "
                "current board",
                ticket.id,
                ", ".join(absent),
                ", ".join(local),
            )
            # Best-effort: file a consumer follow-up on the mill board.
            try:
                mill_svc = resolve_mill_service(s, ctx.service, caller_label="refine")
            except Exception:
                log.debug(
                    "%s: resolve_mill_service raised during follow-up — "
                    "skipping consumer follow-up",
                    ticket.id,
                    exc_info=True,
                )
                mill_svc = None

            if mill_svc is not None and mill_svc.board_id != ctx.service.board_id:
                followup_title = f"Consumer migration for: {ticket.title}"
                followup_body = (
                    f"Consumer follow-up for ticket {ticket.id} "
                    f"({ticket.title}).\n\n"
                    f"The primary deliverable lives on another checkout; "
                    f"this ticket tracks the consumer-side migration.\n\n"
                    f"Absent mill consumer paths:\n"
                    + "\n".join(f"- `{p}`" for p in absent)
                    + f"\n\n{REFINE_MILL_CONSUMER_FOLLOWUP_PREFIX} "
                    f"for {ticket.id}"
                )
                try:
                    mill_svc.create(
                        followup_title,
                        followup_body,
                        source=ticket.source,
                        origin_session=ticket.origin_session,
                    )
                    log.info(
                        "%s: filed mill consumer follow-up for absent paths: %s",
                        ticket.id,
                        ", ".join(absent),
                    )
                except Exception:
                    log.info(
                        "%s: failed to create consumer follow-up on mill "
                        "board — proceeding with refine on current board",
                        ticket.id,
                        exc_info=True,
                    )
            else:
                log.debug(
                    "%s: mill board not available or same as current — "
                    "skipping consumer follow-up",
                    ticket.id,
                )
            return None

        # No local deliverable — the whole actionable scope is mill
        # work absent here.  Preserve the existing full-redirect
        # behaviour.
        try:
            mill_svc = resolve_mill_service(s, ctx.service, caller_label="refine")
        except Exception:
            log.warning(
                "%s: resolve_mill_service raised — proceeding with refine",
                ticket.id,
                exc_info=True,
            )
            return None

        if mill_svc is None:
            log.warning(
                "%s: mill board not configured — cannot redirect; "
                "proceeding with refine",
                ticket.id,
            )
            return None

        if mill_svc.board_id == ctx.service.board_id:
            # Already on the mill board — nothing to redirect.
            log.debug(
                "%s: already on the mill board (%s) — proceeding with refine",
                ticket.id,
                mill_svc.board_id,
            )
            return None

        try:
            new = mill_svc.create(
                ticket.title,
                draft,
                source=ticket.source,
                origin_session=ticket.origin_session,
            )
        except Exception:
            log.warning(
                "%s: failed to create draft on mill board — proceeding with refine",
                ticket.id,
                exc_info=True,
            )
            return None

        return Outcome(
            State.DONE,
            f"{REFINE_MILL_MISROUTE_PREFIX} {new.id}: draft names mill "
            f"paths absent from this checkout ({', '.join(absent)})",
        )

    @staticmethod
    def _run_doc_only_gate(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        title: str,
        ws: Workspace,
        s: Settings,
    ) -> Outcome | None:
        """Deterministic doc-only gate — skip refine for documentation-only changes.

        When *auto_approve_enabled* and every file path extracted from
        the draft is a docs/Markdown path (``docs/**``, ``*.md``,
        ``CHANGELOG.md``) with no code/config files (``.py``, ``.ts``,
        ``.js``, ``.yaml``, ``.yml``), short-circuit directly to READY
        with a templated verdict — no LLM calls.  Returns ``None``
        when the draft is not doc-only or *auto_approve_enabled* is off
        (fall through to normal refine).
        """
        if not s.auto_approve_enabled:
            return None

        from .helpers import _is_doc_only_change
        from . import _reconcile

        if not _is_doc_only_change(draft, title):
            return None

        # Mirror the artifact writes from _triage_outcome for
        # traceability (draft-original.md + empty file_map.json).
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft if draft else "(title-only ticket, no body provided)",
            encoding="utf-8",
        )
        _reconcile.write_file_map(ws, [], only_if_absent=True)

        return Outcome(
            State.READY,
            "Documentation-only change; no code review needed",
        )
