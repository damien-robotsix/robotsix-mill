"""Preflight gate checks for the implement phase.

Extracted from :class:`PhaseCoordinatorMixin` to reduce file size.
All nine gate checks run BEFORE a Langfuse trace opens, catching
known-no-op conditions without consuming a spawn slot or emitting
a $0.00 trace.
"""

from __future__ import annotations

import hashlib
import json

from robotsix_mill._resources import (
    effective_language_instructions_dir,
    effective_skills_dir,
)

from ..._resources import agent_definitions_dir
from ...agents.yaml_loader import load_agent_definition
from ...core.models import Ticket, TicketKind
from ...core.states import State
from ...deploy import check_deploy_freshness
from ..base import Outcome, StageContext
from ..pause import clear_conversation_state
from ._shared import log


def run_preflight_checks(
    ticket: Ticket,
    ctx: StageContext,
) -> Outcome | None:
    """Cheap checks that can gate implement BEFORE a Langfuse trace opens.

    Catches known-no-op conditions (empty spec, spawn limit, cycle
    limit) without consuming a spawn slot or emitting a $0.00 trace.
    """
    s = ctx.settings
    ws = ctx.service.workspace(ticket)

    # 0. Epic guard: implement is for TASK tickets only.  An epic
    #    reaching this stage signals a dispatch bug — block it
    #    before any trace opens so a human can triage.
    if ticket.kind == TicketKind.EPIC:
        return Outcome(
            State.BLOCKED,
            "epic ticket routed to implement stage — epics must "
            "be broken into child tasks; re-route to epic_breakdown "
            "or refine for child generation",
        )

    # 0.5. Deploy-freshness gate: when the deploy server reports an
    #      image update is available, the running worker predates the
    #      latest commit.  Any implement attempt on stale code risks
    #      reproducing bugs already fixed in the newer image.  Park
    #      the ticket with explicit digest info so the operator can
    #      trigger a redeploy before retrying.
    deploy_status = check_deploy_freshness(s.deploy_api_url)
    if deploy_status is not None and deploy_status.update_available:
        return Outcome(
            State.BLOCKED,
            f"worker image is stale — running {deploy_status.running_digest} "
            f"predates latest {deploy_status.latest_digest}.  "
            "Redeploy the mill worker before resuming blocked tickets.",
        )

    # 1. Spec must exist and be non-empty — without a spec the agent
    #    has nothing to implement and would return empty/no-op.
    #    Tickets with a parent epic inherit their spec from the epic
    #    context — only block when BOTH the direct spec and the epic
    #    context are empty.
    spec = ws.read_description()
    if not spec or not spec.strip():
        epic_ctx = ctx.service.get_epic_context(ticket)
        if not epic_ctx or not epic_ctx.strip():
            return Outcome(
                State.BLOCKED,
                "empty or missing specification — cannot implement without a spec",
            )

    # 2. Implement spawn counter: cap the total number of
    #    implement-stage invocations per ticket so that a ticket
    #    stuck in a BLOCKED→READY→BLOCKED loop cannot burn
    #    unbounded LLM quota across re-spawns.  Counted and gated
    #    here in preflight so a ticket at the spawn limit fails
    #    fast with BLOCKED before a Langfuse trace opens.
    spawn_limit = s.implement_max_spawns_per_ticket
    if spawn_limit > 0:
        counter_path = ws.artifacts_dir / "implement_spawn_count"
        spawn_count = 0
        if counter_path.exists():
            try:
                spawn_count = int(counter_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):  # fmt: skip
                spawn_count = 0
        if spawn_count >= spawn_limit:
            note = (
                f"implement spawn limit reached "
                f"({spawn_count}/{spawn_limit}) — "
                "escalating to BLOCKED for human inspection.  "
                "Delete artifacts/implement_spawn_count in the "
                "workspace to reset."
            )
            # Append the tail of the last implement summary so the
            # operator sees the genuine failure cause instead of only
            # the generic limit message.
            summary_path = ws.artifacts_dir / "implement_summary.md"
            if summary_path.exists():
                try:
                    summary_text = summary_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    summary_text = ""
                if summary_text:
                    tail = summary_text[-500:].strip()
                    if tail:
                        note += f"\n\nLast attempt summary tail:\n{tail}"
            # Discard any stale conversation state so a
            # resume-blocked restart begins a fresh agent
            # conversation instead of replaying the prior
            # transcript.
            clear_conversation_state(ws, "implement")
            return Outcome(State.BLOCKED, note)
        # Only increment on genuine re-spawns, not transient
        # infrastructure retries.  Transient failures (sandbox EOF,
        # OOM, etc.) must not burn the ticket's spawn budget —
        # otherwise a single flaky runner can permanently deadlock
        # a ticket against the spawn limit.
        if ticket.retry_attempt == 0:
            spawn_count += 1
            try:
                counter_path.write_text(str(spawn_count), encoding="utf-8")
            except OSError:
                log.warning(
                    "%s: failed to write implement_spawn_count",
                    ticket.id,
                    exc_info=True,
                )

    # 3. Ticket-lifetime implement-cycle cap: catch the runaway
    #    implement↔review loop before we clone or open a trace.
    if (
        s.max_implement_review_cycles > 0
        and ticket.implement_cycles >= s.max_implement_review_cycles
    ):
        return Outcome(
            State.BLOCKED,
            f"Implement-review cycle limit reached "
            f"({ticket.implement_cycles}/{s.max_implement_review_cycles}) — "
            "escalating to BLOCKED for human inspection",
        )

    # 4. Stale re-spawn guard: if the last implement attempt was not
    #    successful ("BLOCKED — resumable") and the effective spec
    #    (direct description + epic context) hasn't changed since
    #    that attempt, re-spawning would produce the same result.
    #    Fail fast before a trace opens to prevent the $0.00 trace /
    #    no-op re-spawn pattern.
    implement_md = ws.artifacts_dir / "implement.md"
    if implement_md.exists():
        try:
            md_content = implement_md.read_text(encoding="utf-8")
        except OSError:
            md_content = ""
        if "BLOCKED — resumable" in md_content:
            # Assemble the effective spec the same way
            # _load_implement_context does (epic context first,
            # then direct description).
            effective = spec or ""
            if ticket.parent_id:
                epic_ctx2 = ctx.service.get_epic_context(ticket)
                if epic_ctx2:
                    effective = epic_ctx2 + "\n\n" + effective
            current_fp = hashlib.sha256(effective.encode("utf-8")).hexdigest()[:16]
            # Extract stored fingerprint from implement.md.
            stored_fp = ""
            for line in md_content.splitlines():
                if line.startswith("spec-fingerprint: "):
                    stored_fp = line.split("spec-fingerprint: ", 1)[1].strip()
                    break
            if stored_fp and stored_fp == current_fp:
                return Outcome(
                    State.BLOCKED,
                    "spec unchanged since last spec-determined "
                    "implement attempt "
                    f"(fingerprint {current_fp}) — "
                    "re-implementing would produce the same "
                    "result.  Update the specification to change "
                    "the fingerprint, or force a retry via "
                    "resume-blocked with a justification note, or "
                    "use the reset-fingerprint endpoint to clear "
                    "the guard.",
                )

    # 4.5. Cross-spawn stall guard: if a prior implement cycle
    #      already tripped the stall detector (summary unchanged
    #      across consecutive BLOCKED attempts despite open review
    #      feedback), block BEFORE incrementing the spawn counter
    #      so a manual resume doesn't silently burn another round.
    #      The stall state lives in implement.md and, for continuity
    #      across resume-blocked cycles, in implement_stall_state.json.
    _stall_count = 0
    _stall_summary = ""
    if implement_md.exists():
        try:
            _md_stall = implement_md.read_text(encoding="utf-8")
        except OSError:
            _md_stall = ""
        for _line in _md_stall.splitlines():
            if _line.startswith("stall-count: "):
                try:
                    _stall_count = int(_line.split("stall-count: ", 1)[1].strip())
                except ValueError:
                    _stall_count = 0
                break
    # Fall back to the persisted JSON stall state — survives
    # _clear_stale_implement_guard on resume-blocked.
    if _stall_count == 0:
        _ss_path = ws.artifacts_dir / "implement_stall_state.json"
        if _ss_path.exists():
            try:
                _ss = json.loads(_ss_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _ss = {}
            _stall_count = _ss.get("stall_count", 0)
    if _stall_count > 0:
        _threshold = getattr(s, "implement_stall_threshold", 2)
        if _threshold > 0 and _stall_count >= _threshold:
            # Surface the stall diagnostic from the last
            # attempt's summary — it already includes review
            # comment ids and the recommended remedy.
            _summary_path = ws.artifacts_dir / "implement_summary.md"
            if _summary_path.exists():
                try:
                    _stall_summary = _summary_path.read_text(encoding="utf-8").strip()
                except OSError:
                    # _stall_summary is already ""; the file is
                    # non-critical — swallow and continue.
                    pass
            if _stall_summary and _stall_summary.startswith("STALL DETECTED"):
                return Outcome(State.BLOCKED, _stall_summary)
            return Outcome(
                State.BLOCKED,
                f"stall guard — {_stall_count} consecutive "
                "no-progress implement cycles detected.  "
                "The implement agent is not converging.  "
                "Consider re-scoping or splitting the ticket.",
            )

    # 5. Agent tool-definition integrity: the assembled tool list
    #    must be non-empty before we open a trace.  Load the agent-
    #    definition YAML and verify it declares at least one tool.
    #    An empty tools list signals a misconfigured or corrupted
    #    agent definition that would produce a no-op agent with no
    #    ability to explore, read, or edit.
    try:
        definition = load_agent_definition(agent_definitions_dir() / "implement.yaml")
    except Exception as exc:
        return Outcome(
            State.BLOCKED,
            f"failed to load implement agent definition: {exc}",
        )
    if not definition.tools:
        return Outcome(
            State.BLOCKED,
            "implement agent definition has no tools configured — "
            "the tools list in agent_definitions/implement.yaml "
            "is empty",
        )

    # 6. Skill-file integrity: every skill referenced by the agent
    #    definition must exist on disk before the model runs.  A
    #    missing skill silently degrades the system prompt (the
    #    ``compose_prompt`` warning is invisible to the model) and
    #    produces a no-op loop.  Resolved through the same
    #    packaged-dir fallback ``compose_prompt`` uses, so a stale
    #    CWD-relative ``skills_dir`` override degrades to the
    #    bundled skills instead of hard-blocking every ticket
    #    (2026-07-19: a relative override bricked the whole board,
    #    including the ticket that would have fixed the config).
    skills_root = effective_skills_dir(s.skills_dir)
    for name in definition.skills or ():
        skill_path = skills_root / name / "SKILL.md"
        if not skill_path.is_file():
            return Outcome(
                State.BLOCKED,
                f"missing skill file: {skill_path}",
            )

    # 7. Language-instruction integrity: a missing built-in snippet
    #    directory (e.g. ``agent_definitions/language_instructions``)
    #    silently returns ``""`` for every language, degrading the
    #    prompt for every non-mill repo that declares a language.
    #    Checked through the packaged-dir fallback the snippet
    #    loader uses, so only a genuinely unresolvable directory
    #    blocks.
    if not effective_language_instructions_dir(s.language_instructions_dir).is_dir():
        return Outcome(
            State.BLOCKED,
            f"language_instructions_dir not found or not a directory: "
            f"{s.language_instructions_dir}",
        )

    # 8. Workspace integrity: the ticket workspace directory must
    #    be present and accessible.  If the workspace root has been
    #    deleted or the filesystem is unavailable, fail fast
    #    instead of spinning a model pass that cannot persist
    #    artifacts.
    if not ws.dir.exists() or not ws.dir.is_dir():
        return Outcome(
            State.BLOCKED,
            f"workspace directory absent or inaccessible: {ws.dir}",
        )

    return None
