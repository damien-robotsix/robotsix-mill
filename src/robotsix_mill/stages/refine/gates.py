"""Pre-refine gate phases for the refine stage.

A mixin (:class:`RefineGatesMixin`) holding the cheap, deterministic-or-
single-LLM-call guards that run *before* the expensive refine agent:
dedup / already-done, in-flight advisory, freshness, and obsolescence.
These are mixed into :class:`RefineStage` (in ``core.py``); they call the
pure helpers from :mod:`.helpers` and the agent modules from
:mod:`...agents`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ...agents import freshness, obsolescence
from ...config import Settings
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...core.workspace import Workspace
from ..base import Outcome, StageContext
from ._gates_dedup import (
    _is_valid_dedup_target,
    _run_dedup_guard,
    _run_inflight_advisory,
    _verify_advisory_dedup,
)
from .helpers import (
    FRESHNESS_STALE_PREFIX,
    OBSOLESCENCE_GAP_PREFIX,
    SCOPE_TRIAGE_REPO_AWARENESS_GATE_PREFIX,
    STANDARDS_GATE_PREFIX,
    WORKFLOW_PORTABILITY_GATE_PREFIX,
    log,
)

# Distinctive mill-pipeline tokens.  When one of these appears in a draft
# but is absent from the target repo, the draft almost certainly targets
# the mill itself and was filed against the wrong board.  These are
# deliberately narrow — single generic English words ("fix", "test",
# "config", "implement", "review", …) are handled separately via the
# stage-phrase patterns below, never matched bare.
_SCOPE_TRIAGE_DOMAIN_TOKENS: tuple[str, ...] = (
    "ci_fix",
    "ci_fix_codeql",
    "ci_transient",
    "dependency_fix",
    "scope_triage",
    "scope-triage",
    "epic_breakdown",
    "epic_status",
    "doc_classifier",
    "spec-review",
    "codeql_fp_triage",
    "review_revision",
    "reviewer-agreement",
    "meta_triage",
    "robotsix_mill",
    "robotsix-mill",
    "file_map",
    "max_memory_chars",
    "coordinator_max_tool_calls",
    "run_coordinator",
    "agent_definitions",
)

# Mill stage names that are also ordinary English words.  These are only
# flagged when used in the mill sense — "<name> stage" / "<name> agent" —
# so a draft about "implementing a feature" or a "document update" is not
# mistaken for a mill mis-route.
_SCOPE_TRIAGE_STAGE_NAMES: tuple[str, ...] = (
    "ci_fix",
    "ci_fix_codeql",
    "ci_transient",
    "dependency_fix",
    "scope_triage",
    "scope-triage",
    "epic_breakdown",
    "epic_status",
    "refine",
    "triage",
    "implement",
    "review",
    "document",
    "deliver",
    "answer",
    "retrospect",
    "merge",
    "rebase",
)

_SCOPE_TRIAGE_STAGE_PHRASE_RE = re.compile(
    r"\b("
    + "|".join(re.escape(name) for name in _SCOPE_TRIAGE_STAGE_NAMES)
    + r")\s+(?:stage|agent)\b",
    re.IGNORECASE,
)


class RefineGatesMixin:
    """Pre-refine gate staticmethods mixed into :class:`RefineStage`."""

    # Dedup & in-flight advisory cluster lives in ._gates_dedup; re-imported
    # here so RefineGatesMixin (and RefineStage) keep the same staticmethod API.
    _run_dedup_guard = staticmethod(_run_dedup_guard)
    _is_valid_dedup_target = staticmethod(_is_valid_dedup_target)
    _run_inflight_advisory = staticmethod(_run_inflight_advisory)
    _verify_advisory_dedup = staticmethod(_verify_advisory_dedup)

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
    def _run_workflow_portability_gate(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        title: str,
    ) -> Outcome | None:
        """Deterministic workflow-portability gate — reject tickets that
        propose enabling an internal (non-portable) periodic workflow on a
        non-mill repo.

        Inspects the draft for ``.robotsix-mill/periodic/<name>.yaml``
        presence-file patterns and cross-references every matched
        ``<name>`` against the data-driven portability map.  Internal
        workflows proposed for managed repos are short-circuited to
        ``DONE`` before any LLM budget is spent.

        Returns ``None`` when the draft does not propose an internal
        workflow on a non-mill repo (fall through to normal refine).
        """
        import re

        # Only gate non-mill boards — internal workflows ARE valid for mill.
        if ticket.board_id == "robotsix-mill":
            return None

        text = f"{title}\n{draft}"

        for m in re.finditer(r"\.robotsix-mill/periodic/([a-z][a-z0-9_]*)\.yaml", text):
            name = m.group(1)
            from ...agents.workflow_portability import _BUILTIN_KINDS, is_portable

            if name in _BUILTIN_KINDS and not is_portable(name):
                log.info(
                    "%s: workflow-portability gate — draft proposes enabling "
                    "internal workflow %r on non-mill board %r; short-circuiting",
                    ticket.id,
                    name,
                    ticket.board_id,
                )
                return Outcome(
                    State.DONE,
                    f"{WORKFLOW_PORTABILITY_GATE_PREFIX} {name!r} is not portable — "
                    "cannot be enabled on a managed repo via a "
                    ".robotsix-mill/periodic/ presence file",
                )

        return None

    @staticmethod
    def _run_scope_triage_repo_awareness_gate(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        title: str,
        repo_dir: Path | None,
    ) -> Outcome | None:
        """Deterministic repo-awareness gate — reject mis-routed drafts.

        Extracts mill-pipeline domain terms from the spec (distinctive
        tokens such as ``ci_fix`` and stage-name phrases such as
        ``implement stage``) and greps the target repo for each one.
        A term that appears in the spec but not anywhere in the repo
        means the draft targets a mill construct the repo does not
        contain — the ticket was almost certainly filed against the
        wrong board, so it is short-circuited before any LLM budget is
        spent.

        Generic words ("fix", "test", "config", …) are never flagged;
        only curated mill-specific tokens and ``<stage> stage`` /
        ``<stage> agent`` phrases are checked, so a draft about adding
        a new feature or fixing a bug passes through untouched.

        Returns ``None`` when no domain term is detected, the repo
        cannot be grepped, or every detected term is present in the
        repo (fall through to normal refine).
        """
        # Meta-board tickets are cross-repo by design: the mill concepts
        # they cite legitimately live in one of the OTHER cloned repos,
        # so a single-repo grep cannot prove a mis-route.  Skip.
        if ticket.board_id == "meta":
            return None

        text = f"{title}\n{draft}"

        needles: list[str] = []
        for token in _SCOPE_TRIAGE_DOMAIN_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE):
                needles.append(token)
        needles.extend(m.group(0) for m in _SCOPE_TRIAGE_STAGE_PHRASE_RE.finditer(text))

        if not needles:
            return None

        # Cannot prove a mis-route without a repo to grep — fall through
        # and let the triage classifier (which has explore/read_file
        # tools) handle grounding verification.
        if repo_dir is None or not (repo_dir / ".git").exists():
            return None

        for needle in needles:
            result = subprocess.run(
                ["git", "-C", str(repo_dir), "grep", "-q", "-I", "-i", "-e", needle],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                continue  # term exists in the repo — not a mis-route
            if result.returncode == 1:
                log.info(
                    "%s: scope-triage repo-awareness gate — %r appears in "
                    "the spec but not in the repo; rejecting as mis-routed",
                    ticket.id,
                    needle,
                )
                return Outcome(
                    State.DONE,
                    f"{SCOPE_TRIAGE_REPO_AWARENESS_GATE_PREFIX} {needle!r} is a "
                    "mill pipeline concept that does not appear in this "
                    "repository — the ticket was likely filed against the "
                    "wrong board; rejecting before implement",
                )
            # Any other exit code is a git error (e.g. no commits yet) —
            # treat as "cannot verify" and keep checking / fall through.
            log.debug(
                "%s: scope-triage repo-awareness grep for %r errored (%d) — "
                "falling through",
                ticket.id,
                needle,
                result.returncode,
            )

        return None

    @staticmethod
    def _run_standards_gate(
        ctx: StageContext,
        ticket: Ticket,
        draft: str,
        title: str,
        s: Settings,
    ) -> Outcome | None:
        """Run the LLM-based fleet-standards gate (best-effort).

        For repos that follow robotsix-standards
        (:meth:`RepoConfig.follows_robotsix_standards`), a single cheap
        LLM call judges whether the draft's GOAL conflicts with an
        explicit standards prohibition — e.g. "publish @robotsix/ui to
        npm" against distribution-packaging.md's no-registry rule.  A
        clear violation is short-circuited to ``DONE`` with the citation
        before any refine LLM budget is spent.

        Returns ``None`` (proceed) when the gate is disabled, the repo
        does not follow the standards, the draft is trivial or
        user-authored, the check fails, or no violation is found.
        User-authored drafts reflect deliberate human intent and are
        never auto-closed — the operator can consciously depart from a
        standard; the gate targets the agent-spawned drafts
        (retrospect, trace-review, chat) that propose off-standards
        work nobody asked for.
        """
        if not s.standards_gate_enabled:
            return None

        repo_config = ctx.repo_config
        if repo_config is None or not repo_config.follows_robotsix_standards():
            return None

        if not draft or len(draft) < 50:
            log.debug(
                "%s: trivial draft (%d chars), skipping standards gate",
                ticket.id,
                len(draft or ""),
            )
            return None

        if ticket.source == SourceKind.USER:
            log.debug(
                "%s: user-authored draft, skipping standards gate",
                ticket.id,
            )
            return None

        from ...agents import standards_gate

        try:
            result = standards_gate.run_standards_gate_check(
                settings=s,
                draft_title=title,
                draft_body=draft,
            )
        except Exception:
            log.warning(
                "%s: standards gate check failed, proceeding with refine",
                ticket.id,
                exc_info=True,
            )
            return None

        if result.get("violates"):
            standard = result.get("standard", "robotsix-standards")
            reason = result.get("reason", "draft goal violates a fleet standard")
            log.info(
                "%s: standards gate flagged violation of %s — %s",
                ticket.id,
                standard,
                reason,
            )
            # Discarded drafts go to DONE so retrospect still analyses
            # them — same pattern as the freshness/dedup/obsolescence
            # gates.
            return Outcome(
                State.DONE,
                f"{STANDARDS_GATE_PREFIX} draft conflicts with {standard} — {reason}",
            )

        log.debug(
            "%s: standards gate passed — %s",
            ticket.id,
            result.get("reason", ""),
        )
        return None

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

        from . import _reconcile
        from .helpers import _is_doc_only_change

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
