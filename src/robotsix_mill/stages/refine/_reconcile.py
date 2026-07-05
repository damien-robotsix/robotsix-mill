"""Artifact side-effects, short-circuit guards, and shared utilities.

Writes artifacts (file_map, triage_complexity), applies agent output
side-effects (memory, title, epic body, draft, file_map, reference_files),
and provides the short-circuit guards (reviewer-agreement, gitignored-path,
internal-toolchain-failure) that gate the expensive refine-agent call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...agents import refining
from ...config.settings import Settings
from ...core.models import Ticket, TicketKind
from ...core.states import State
from ...core.workspace import Workspace
from ...vcs import git_ops
from ..base import Outcome, StageContext
from .helpers import (
    log,
)
from . import _result_paths


# ---------------------------------------------------------------------------
# triage complexity / findings I/O
# ---------------------------------------------------------------------------


def write_triage_complexity(
    ws: Workspace,
    complexity: str,
    trivial_scope: bool | None = None,
    findings: str | None = None,
) -> None:
    """Persist the triage complexity verdict (and optionally the trivial-scope
    flag and exploration findings) for downstream consumption."""
    data: dict[str, Any] = {"complexity": complexity}
    if trivial_scope is not None:
        data["trivial_scope"] = trivial_scope
    (ws.artifacts_dir / "triage_complexity.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    if findings:
        (ws.artifacts_dir / "triage_findings.json").write_text(
            json.dumps({"findings": findings}), encoding="utf-8"
        )


def read_triage_complexity(ws: Workspace) -> str:
    """Read the triage complexity verdict; returns ``"needs-exploration"``
    when the file is absent (conservative default)."""
    path = ws.artifacts_dir / "triage_complexity.json"
    if not path.exists():
        return "needs-exploration"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("complexity", "needs-exploration"))
    except json.JSONDecodeError, KeyError:
        return "needs-exploration"


def read_triage_findings(ws: Workspace) -> str | None:
    """Read the triage exploration findings; returns ``None`` when the
    artifact is absent or unparseable (conservative default — no block)."""
    path = ws.artifacts_dir / "triage_findings.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        findings = data.get("findings")
        return str(findings) if findings else None
    except json.JSONDecodeError, KeyError:
        return None


def read_triage_trivial(ws: Workspace) -> bool:
    """Read the triage trivial-scope verdict; returns ``False`` when the
    file or key is absent (conservative default — no cheap-model routing)."""
    path = ws.artifacts_dir / "triage_complexity.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("trivial_scope", False))
    except json.JSONDecodeError, KeyError:
        return False


def persist_triage_complexity(
    ws: Workspace,
    triage: Any,
) -> None:
    """Persist the triage complexity verdict for downstream exploration gating."""
    complexity = triage.complexity
    if complexity is None:
        complexity = "needs-exploration"
    write_triage_complexity(
        ws,
        complexity,
        trivial_scope=triage.trivial_scope,
        findings=triage.exploration_findings,
    )


# ---------------------------------------------------------------------------
# shared artifact helpers (used by multiple sub-modules)
# ---------------------------------------------------------------------------


def write_file_map(
    ws: Workspace, entries: list[dict[str, str]], *, only_if_absent: bool = False
) -> None:
    """Write ``file_map.json`` to the workspace artifacts dir.

    *entries* is a list of ``{"file": ..., "note": ...}`` dicts (``[]``
    renders as the empty file map). When *only_if_absent* is set, an
    existing file is left untouched — the scope-free / triage-skip
    behaviour that must not clobber a previously written map.
    """
    file_map_path = ws.artifacts_dir / "file_map.json"
    if only_if_absent and file_map_path.exists():
        return
    file_map_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# phase: short-circuit internal toolchain failures
# ---------------------------------------------------------------------------


def short_circuit_for_internal_failure(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
    reviewer_comments: str | None,
) -> Outcome | None:
    """Short-circuit refine to a minimal spec for internal toolchain failures.

    When the draft already carries concrete CI/test/type/lint failure
    output, there is no need to re-derive root cause via the expensive
    refine agent — produce a minimal spec that points implement at the
    logged failures.

    Gate conditions (ALL must hold):
    - No reviewer sendback (human-flagged changes always get full refinement)
    - Draft is non-empty
    - ``is_internal_toolchain_failure(draft)`` is ``True``

    Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
    through to the full refine agent.
    """
    if reviewer_comments:
        return None
    if not draft or not draft.strip():
        return None
    if not refining.is_internal_toolchain_failure(draft):
        return None

    log.info(
        "%s: short-circuiting refine — draft carries internal toolchain "
        "failure logs; producing minimal spec for implement",
        ticket.id,
    )

    # Build a minimal spec that points implement at the logged failures.
    evidence_note = ""
    evidence_path = ws.artifacts_dir / "evidence.txt"
    if evidence_path.exists():
        try:
            evidence_text = evidence_path.read_text(encoding="utf-8")[:4000]
            evidence_note = (
                f"\nAdditional evidence from `artifacts/evidence.txt`:\n\n"
                f"```\n{evidence_text}\n```\n"
            )
        except Exception:
            log.warning(
                "%s: failed to read evidence.txt, skipping",
                ticket.id,
                exc_info=True,
            )

    # Truncate the draft body for embedding — keep enough to show the
    # failure but avoid ballooning the spec.
    draft_excerpt = draft[:3000]
    if len(draft) > 3000:
        draft_excerpt += "\n… [truncated]"

    spec = (
        "## Problem\n\n"
        "An internal toolchain failure (CI/type/lint/test) was detected. "
        "The draft already carries the failing logs — fix locally so the "
        "check passes.\n\n"
        "## Scope\n\n"
        "Fix the failing check. The draft body contains the error details:\n\n"
        f"```\n{draft_excerpt}\n```\n"
        f"{evidence_note}"
        "\n## Acceptance criteria\n\n"
        "- The failing check passes.\n\n"
        "## Out of scope / constraints\n\n"
        "- Do not expand scope beyond fixing this specific toolchain failure.\n"
        "- This is a local code/config fix — no external investigation needed.\n"
    )

    # Persist the raw draft if not already preserved.
    draft_original = ws.artifacts_dir / "draft-original.md"
    if not draft_original.exists():
        draft_original.write_text(draft, encoding="utf-8")

    # Write the minimal spec to the workspace description so implement
    # picks it up (unlike the triage-skip and split-child paths, which
    # keep the draft as-is, this path produces a new spec).
    new_hash = ws.write_description(spec)
    ctx.service.set_content_hash(ticket.id, new_hash)

    # Write an empty file_map so implement treats this as scope-free mode.
    write_file_map(ws, [], only_if_absent=True)

    # Record complexity so downstream gates don't re-triage.
    write_triage_complexity(ws, "simple")

    return _result_paths.resolved_outcome(
        ctx,
        spec,
        ticket.id,
        "short-circuited refine — internal toolchain failure with logs",
        source=ticket.source,
    )


# -- phase: gitignored file_map guard -----------------------------------


def gitignored_guard(
    ticket: Ticket, result: refining.RefineResult, repo_dir: Path | None
) -> Outcome | None:
    """Reject a spec whose deliverable files target gitignored paths.

    Deterministically reject a spec whose deliverable files target
    paths gitignored in the repo clone (e.g. a manifest board whose
    ``.gitignore`` carries ``/src/*`` for vcs-imported sub-repos).
    Those edits would land on disk but be invisible to git, dying at
    implement as an opaque "no changes produced" block. Catch it here
    — before any memory/title/epic side-effects — with an actionable
    note. Meta/multi-repo workspaces are skipped: a path tracked in
    one clone can look ignored relative to another, and robust
    per-repo resolution belongs with manifest-aware delivery.
    """
    if ticket.board_id != "meta" and result.file_map and repo_dir is not None:
        blocked = git_ops.ignored_paths(repo_dir, [e.file for e in result.file_map])
        if blocked:
            hit_list = ", ".join(f"`{p}`" for p in blocked)
            return Outcome(
                State.BLOCKED,
                f"refine produced a spec targeting gitignored path(s): "
                f"{hit_list}. This board cannot deliver changes there — the "
                "paths are vcs-imported / vendored sub-trees (e.g. `/src/*` "
                "managed via repos.yaml), invisible to git. Re-scope the "
                "spec to target git-tracked files in this repo (e.g. the "
                "manifest / repos.yaml and the board's own sources), not "
                "the cloned workspace sources.",
            )
    return None


# -- phase: persist agent output side-effects ---------------------------


def apply_agent_side_effects(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
    epic_ctx: str,
    result: refining.RefineResult,
) -> None:
    """Persist memory, title, epic body, draft, and artifact files.

    Runs after the gitignored guard for every non-short-circuit path:
    updated memory, an agent-supplied title, the non-split epic body,
    the raw-draft preservation, and the ``file_map`` / ``reference_files``
    artifacts.
    """
    if result.updated_memory:
        # Late import from orchestration so test monkeypatches on
        # orchestration._persist_refine_memory take effect here.
        from . import orchestration as _orch

        memory_board_id = (
            ctx.repo_config.repo_id if ctx.repo_config else ticket.board_id
        )
        _orch._persist_refine_memory(s, memory_board_id, result.updated_memory)

    if result.title and result.title.strip():
        ctx.service.set_title(ticket.id, result.title.strip())

    # --- epic body handling (non-split path) ---
    # In autonomous mode: apply immediately to the epic.
    # In gated mode: store as artifact in child workspace for
    # later application on approval.
    if result.epic_body and result.epic_body.strip() and epic_ctx:
        parent = ctx.service.get(ticket.parent_id)  # type: ignore[arg-type]
        if parent is not None and parent.kind == TicketKind.EPIC:
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
        write_file_map(ws, [{"file": e.file, "note": e.note} for e in result.file_map])

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


# -- phase: reviewer-agreement guard (pre-Opus cost saver) --------------


def reviewer_agreement_guard(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    ws: Workspace,
    s: Settings,
    reviewer_comments: str | None,
) -> Outcome | None:
    """Pre-Opus guard: when reviewer feedback confirms the draft's
    no-change-needed conclusion, short-circuit to DONE — skipping the
    expensive Opus refine agent.

    Gated by ``reviewer_agreement_gate_enabled`` AND
    ``refine_triage_enabled`` (both must be True), and only runs when
    ``reviewer_comments`` is present (truthy).  A single cheap L1
    classifier (DeepSeek flash, ~$0.0003) replaces what would
    otherwise be a full Opus refine call (~$0.28).

    Returns an :class:`Outcome` to short-circuit, or ``None`` to fall
    through to the full pipeline.
    """
    if not (
        s.reviewer_agreement_gate_enabled
        and s.refine_triage_enabled
        and reviewer_comments
    ):
        return None
    try:
        agreement = refining.triage_reviewer_agreement(
            settings=s,
            draft=f"{ticket.title}\n\n{draft}",
            reviewer_comments=reviewer_comments,
        )
    except Exception:
        log.warning(
            "%s: reviewer-agreement triage failed, falling through",
            ticket.id,
            exc_info=True,
        )
        return None

    if agreement.decision != "AGREE":
        return None

    # Reviewer agrees with the draft's conclusion — short-circuit.
    (ws.artifacts_dir / "draft-original.md").write_text(
        draft if draft else "(title-only ticket, no body provided)",
        encoding="utf-8",
    )
    write_file_map(ws, [], only_if_absent=True)
    short = agreement.reason[:400] + ("…" if len(agreement.reason) > 400 else "")

    # A TASK-kind (implementation) ticket that hasn't produced a branch
    # must not be auto-closed from DRAFT.  Route it toward READY so
    # implement can verify the claim against the live tree.
    if ticket.kind == TicketKind.TASK and not ticket.branch:
        return _result_paths.resolved_outcome(
            ctx,
            draft,
            ticket.id,
            f"reviewer agreement — routing to implement: {short}",
            source=ticket.source,
        )

    return Outcome(
        State.DONE,
        f"reviewer agreement — no change needed: {short}",
    )
