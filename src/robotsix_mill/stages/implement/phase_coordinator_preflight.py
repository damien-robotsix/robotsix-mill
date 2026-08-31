"""Preflight gate checks for the implement phase.

Extracted from :class:`PhaseCoordinatorMixin` to reduce file size.
All gate checks run BEFORE a Langfuse trace opens, catching
known-no-op conditions without consuming a spawn slot or emitting
a $0.00 trace.
"""

from __future__ import annotations

import hashlib
import json
import re

from robotsix_mill._resources import (
    effective_language_instructions_dir,
    effective_skills_dir,
)

from ..._resources import agent_definitions_dir
from ...agents.runners.diagnostic_events import emit_diagnostic_event
from ...agents.yaml_loader import load_agent_definition
from ...core.constants import EXTERNAL_SCOPE_PREFIX
from ...core.models import Ticket, TicketKind
from ...core.states import State
from ...core.workspace import (
    Workspace,
    read_spawn_exhaustion_marker,
    record_spawn_exhaustion_marker,
)
from ...deploy import check_deploy_freshness
from ..base import Outcome, StageContext
from ..pause import clear_conversation_state
from ._shared import (
    ZERO_DIFF_PAUSE_FILENAME,
    detect_and_absorb_killed_spawn,
    log,
    read_spawn_aborts_tail,
    read_zero_diff_count,
    write_spawn_in_flight,
    write_zero_diff_count,
)

# ── tool-output capture on spawn-limit exhaustion ──────────────────────

# Number of tool-return outputs to capture from the conversation state
# before it is discarded on spawn-limit exhaustion.  These are written
# to a durable artifact so the operator can see raw tool errors (ruff,
# module-registration, audit) instead of only the model's self-reported
# summary tail.
_TOOL_OUTPUT_CAPTURE_COUNT = 10


def _capture_tool_outputs_from_conversation_state(
    ws: Workspace, max_outputs: int = _TOOL_OUTPUT_CAPTURE_COUNT
) -> str | None:
    """Extract the last *max_outputs* tool-return outputs from the
    implement conversation state and write them to a durable artifact
    (``artifacts/implement_tool_outputs.md``).

    Returns a short tail string for the block note (last 3 outputs,
    capped at 800 chars), or ``None`` if no conversation state existed
    or contained no tool outputs.
    """
    state_path = ws.artifacts_dir / "implement_conversation_state.json"
    if not state_path.exists():
        return None

    try:
        raw = state_path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):  # fmt: skip
        log.warning(
            "Failed to read implement conversation state for tool-output capture"
        )
        return None

    # Normalize to a list of message dicts.  The pydantic-ai format is a
    # top-level list; some tests write a {"messages": [...]} envelope.
    if isinstance(data, dict):
        messages = data.get("messages", [])
    elif isinstance(data, list):
        messages = data
    else:
        return None

    # Collect all tool-return parts across all messages.
    tool_outputs: list[str] = []
    for msg in messages:
        for part in msg.get("parts", []):
            if part.get("part_kind") == "tool-return":
                content = part.get("content", "")
                if isinstance(content, str) and content.strip():
                    tool_outputs.append(content.strip())
                elif isinstance(content, list):
                    # Content-parts list (e.g. pydantic-ai >=0.0.50) —
                    # extract text portions.
                    text_parts: list[str] = []
                    for cp in content:
                        if isinstance(cp, dict) and cp.get("type") == "text":
                            text_parts.append(cp.get("text", ""))
                    combined = "".join(text_parts).strip()
                    if combined:
                        tool_outputs.append(combined)

    if not tool_outputs:
        return None

    # Take the last *max_outputs*.
    last_outputs = tool_outputs[-max_outputs:]

    # Write to durable artifact so the operator can inspect raw errors.
    artifact_path = ws.artifacts_dir / "implement_tool_outputs.md"
    lines: list[str] = ["# Implement tool outputs (last attempt)\n"]
    for i, output in enumerate(last_outputs, 1):
        lines.append(f"## Output {i}\n\n```\n{output}\n```\n")
    try:
        artifact_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        log.warning("Failed to write implement tool outputs artifact")

    # Return a brief tail for the block note — last 3 outputs,
    # capped so the note doesn't overflow the ticket comment limit.
    tail_lines = last_outputs[-3:]
    tail = "\n\n".join(tail_lines)
    return tail[-800:]


def _detect_external_scope(spec: str, ctx: StageContext) -> str | None:
    """Detect when a spec's actionable sections reference only external repos.

    Parses the ``## Scope`` and ``## Acceptance criteria`` sections of
    the spec and looks for references to known repo IDs (from the
    repos registry).  If *every* referenced repo is external (i.e.
    not the current workspace repo), returns a BLOCKED note string.
    Returns ``None`` when the spec references the current repo, when
    no external repos are referenced, or when detection is
    inapplicable (no registry, no repo_id).
    """
    from ...config import get_repos_config

    current_repo_id = ctx.repo_config.repo_id if ctx.repo_config else ""
    if not current_repo_id:
        return None

    # Build the set of known external repo IDs.
    try:
        registry = get_repos_config()
    except Exception:
        # Cannot load registry — skip detection rather than blocking
        # on a config issue.
        return None
    external_ids: set[str] = {rid for rid in registry.repos if rid != current_repo_id}
    if not external_ids:
        return None

    # Extract the Scope and Acceptance criteria sections from the spec.
    # These are the actionable parts — references in the Problem section
    # may describe external context without implying the fix lives there.
    actionable = _extract_actionable_sections(spec)
    if not actionable:
        return None

    # Find which external repos are referenced in the actionable sections.
    referenced_external: set[str] = set()
    for rid in external_ids:
        if re.search(rf"\b{re.escape(rid)}\b", actionable):
            referenced_external.add(rid)

    if not referenced_external:
        return None

    # Check whether the current repo is ALSO referenced in the
    # actionable sections.  If it is, the spec has mixed scope —
    # the implement agent may have local work to do.
    if re.search(rf"\b{re.escape(current_repo_id)}\b", actionable):
        return None

    # Every referenced repo is external — the implement agent cannot
    # produce a diff in this workspace.
    repos_str = ", ".join(sorted(referenced_external))
    return (
        f"{EXTERNAL_SCOPE_PREFIX} the spec's Scope / Acceptance criteria "
        f"reference only external repos ({repos_str}) — no changes target "
        f"this workspace ({current_repo_id}).  The implement agent cannot "
        "produce a diff here.  Re-route the ticket to the correct board "
        "or split the external work into a separate ticket."
    )


def _extract_actionable_sections(spec: str) -> str:
    """Extract Scope and Acceptance criteria sections from a spec.

    Returns the concatenated text of ``## Scope`` and
    ``## Acceptance criteria`` (or ``## Acceptance``) headings through
    to the next ``##`` heading or end-of-text.  Returns ``""`` when
    neither section is found.
    """
    parts: list[str] = []
    # Match ## Scope ... and ## Acceptance criteria ... / ## Acceptance ...
    for heading in ("Scope", "Acceptance criteria", "Acceptance"):
        pattern = re.compile(
            rf"^##\s+{re.escape(heading)}\b.*$",
            re.IGNORECASE | re.MULTILINE,
        )
        m = pattern.search(spec)
        if m:
            start = m.end()
            # Find the next ## heading or end of text.
            next_heading = re.search(r"^##\s", spec[start:], re.MULTILINE)
            end = start + next_heading.start() if next_heading else len(spec)
            parts.append(spec[start:end])
    return "\n".join(parts)


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

    # 1.2. External-scope gate: when the spec's Scope / Acceptance
    #      criteria sections reference ONLY external repos (repos
    #      other than the one this workspace implements), the
    #      implement agent cannot produce a diff here.  Block
    #      immediately instead of burning a full trace cycle.
    if spec and ctx.repo_config is not None:
        block_note = _detect_external_scope(spec, ctx)
        if block_note is not None:
            return Outcome(State.BLOCKED, block_note)

    # 1.5. Spawn-kill recovery: detect a stale in-flight marker from
    #      a previous process lifetime BEFORE the spawn-limit check.
    #      Process death / SIGTERM kills the implement thread mid-
    #      flight, leaving the spawn counter incremented but no
    #      outcome recorded.  Absorb the killed attempt (decrement
    #      counter, log the abort) so it doesn't silently burn the
    #      ticket's spawn budget.
    spawn_limit = s.implement_max_spawns_per_ticket
    counter_path = ws.artifacts_dir / "implement_spawn_count"
    kill_note: str | None = None
    if spawn_limit > 0:
        kill_note = detect_and_absorb_killed_spawn(ws.artifacts_dir, counter_path)

    # 2. Implement spawn counter: LIMIT CHECK ONLY.
    #    Cap the total number of implement-stage invocations per
    #    ticket so that a ticket stuck in a BLOCKED→READY→BLOCKED
    #    loop cannot burn unbounded LLM quota across re-spawns.
    #    The limit check runs early (fast-fail before a trace opens)
    #    but the INCREMENT has been moved to the END of this function
    #    — after all other guards pass — so that guards 3–8 (cycle
    #    cap, stale respawn, stall guard, tool/skill/language/
    #    workspace integrity) never consume a spawn slot.  A block
    #    from any of those guards should be free; only a successful
    #    preflight (about to open a trace and do real work) counts.
    if spawn_limit > 0:
        spawn_count = 0
        if counter_path.exists():
            try:
                spawn_count = int(counter_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):  # fmt: skip
                spawn_count = 0
        if spawn_count >= spawn_limit:
            # Determine whether this exhaustion is a recurrence
            # (same effective spec fingerprint) — if so, emit a
            # distinct RECURRING_SPAWN_EXHAUSTION event and adjust
            # the block note so the operator knows a plain resume
            # will no longer auto-grant a fresh budget.
            effective = spec or ""
            if ticket.parent_id:
                epic_ctx_fp = ctx.service.get_epic_context(ticket)
                if epic_ctx_fp:
                    effective = epic_ctx_fp + "\n\n" + effective
            spec_fp = hashlib.sha256(effective.encode("utf-8")).hexdigest()[:16]
            marker = read_spawn_exhaustion_marker(ws)
            if marker is not None and marker[0] == spec_fp:
                exhaustion_count = marker[1] + 1
                recurring = True
            else:
                exhaustion_count = 1
                recurring = False
            try:
                record_spawn_exhaustion_marker(ws, spec_fp, exhaustion_count)
            except OSError:
                log.warning(
                    "%s: failed to write spawn exhaustion marker",
                    ticket.id,
                )

            if recurring:
                note = (
                    f"implement spawn limit reached "
                    f"({spawn_count}/{spawn_limit}) for the "
                    f"{exhaustion_count}th consecutive time with an "
                    "unchanged spec — recurring spawn exhaustion.  "
                    "Counter will NOT be auto-reset by resume-blocked "
                    "unless the resume includes an explicit "
                    "justification note or the spec has changed."
                )
            else:
                note = (
                    f"implement spawn limit reached "
                    f"({spawn_count}/{spawn_limit}) — "
                    "escalating to BLOCKED for human inspection.  "
                    "Resume-blocked to retry: it clears the counter "
                    "automatically (no workspace surgery needed)."
                )
            # Append the tail of the last implement summary so the
            # operator sees the genuine failure cause instead of only
            # the generic limit message.
            summary_path = ws.artifacts_dir / "implement_summary.md"
            if summary_path.exists():
                try:
                    summary_text = summary_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):  # fmt: skip
                    summary_text = ""
                if summary_text:
                    tail = summary_text[-500:].strip()
                    if tail:
                        note += f"\n\nLast attempt summary tail:\n{tail}"
            # Append kill-recovery evidence (stale in-flight marker
            # from a process death / SIGTERM) so spawn-limit blocks
            # are never evidence-free.
            if kill_note:
                note += f"\n\n{kill_note}"
            aborts_tail = read_spawn_aborts_tail(ws.artifacts_dir)
            if aborts_tail:
                note += f"\n\n{aborts_tail}"
            # Emit a structured diagnostic event so agents
            # (including the periodic diagnostic agent) can
            # discover the exhaustion programmatically and
            # decide whether to request a counter reset or
            # file a deeper bug ticket.
            category = (
                "RECURRING_SPAWN_EXHAUSTION" if recurring else "SPAWN_LIMIT_EXHAUSTED"
            )
            try:
                board_id = ctx.memory_board_id(ticket)
                normalized_key = (
                    "spawn_limit_exhausted:"
                    + hashlib.sha256(
                        f"{ticket.id}:{spawn_count}:{spawn_limit}:{exhaustion_count}".encode()
                    ).hexdigest()[:16]
                )
                emit_diagnostic_event(
                    ctx.settings,
                    board_id,
                    category=category,
                    ticket_id=ticket.id,
                    reason=note,
                    normalized_key=normalized_key,
                )
            except Exception:
                log.exception(
                    "%s: failed to emit %s event",
                    ticket.id,
                    category,
                )
            # Capture raw tool outputs from the conversation state BEFORE
            # clearing it, so the operator can see the actual failing tool
            # errors (ruff, module-registration, audit) instead of only the
            # model's self-reported summary tail.
            tool_output_tail = _capture_tool_outputs_from_conversation_state(ws)
            if tool_output_tail:
                note += f"\n\nLast attempt tool outputs:\n\n{tool_output_tail}"

            # Discard any stale conversation state so a
            # resume-blocked restart begins a fresh agent
            # conversation instead of replaying the prior
            # transcript.
            clear_conversation_state(ws, "implement")
            return Outcome(State.BLOCKED, note)
        # NOTE: the spawn counter INCREMENT is at the end of this
        # function, after all guards pass.  See the block right
        # before ``return None``.

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
    #
    #    The guard is **suppressed** when a matching
    #    ``implement_spec_override`` marker exists — this is written
    #    by ``_clear_stale_implement_guard`` (called when the
    #    operator issues ``resume-blocked`` with a justification
    #    note).  Once overridden, the guard stays suppressed for
    #    that exact spec fingerprint until the spec changes, so the
    #    operator doesn't have to repeatedly call resume-blocked
    #    for the same ticket lifecycle.
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
            # Check whether the operator already overrode the guard
            # for this exact fingerprint — if so, skip re-blocking.
            override_path = ws.artifacts_dir / "implement_spec_override"
            override_fp = ""
            if override_path.exists():
                try:
                    override_fp = override_path.read_text(encoding="utf-8").strip()
                except OSError:
                    override_fp = ""
            if override_fp == current_fp:
                # Operator explicitly overrode — don't re-block.
                pass
            else:
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
            except (json.JSONDecodeError, OSError):  # fmt: skip
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

    # 4.7. Zero-diff early-abort guard: when the N most recent
    #      implement passes all produced no working-tree diff, pause
    #      the ticket with a concrete ask_user prompt instead of
    #      consuming further spawn attempts.  A pass that produces at
    #      least one file change resets the counter.  The zero-diff
    #      count is recorded by ``_finalize`` at each terminal pass,
    #      so its value reflects actual implement attempts (not
    #      preflight blocks).
    zd_threshold = getattr(s, "implement_zero_diff_abort_threshold", 2)
    if zd_threshold > 0:
        zd_count = read_zero_diff_count(ws.artifacts_dir)
        if zd_count >= zd_threshold:
            pause_marker_path = ws.artifacts_dir / ZERO_DIFF_PAUSE_FILENAME
            if pause_marker_path.exists():
                # Operator replied to the zero-diff pause — clear the
                # marker, reset the counter, and allow ONE fresh
                # attempt.
                import contextlib as _ctxlib

                with _ctxlib.suppress(OSError):
                    pause_marker_path.unlink(missing_ok=True)
                write_zero_diff_count(ws.artifacts_dir, 0)
            else:
                # No marker → this is a genuine consecutive-zero-diff
                # streak.  Pause with a concrete prompt so the
                # operator sees the diagnostic before spawn budget is
                # exhausted.
                _note = (
                    f"zero-diff early-abort — {zd_count} consecutive "
                    "implement passes produced no file changes.  "
                    "The task may be a no-op, ambiguous, or require "
                    "external data — please clarify or narrow scope, "
                    "then reply to resume (the reply grants one "
                    "fresh implement attempt with a reset counter)."
                )
                # Write the pause marker so the operator's reply is
                # recognised on the next preflight.
                try:
                    pause_marker_path.touch(exist_ok=True)
                except OSError:
                    log.warning(
                        "%s: failed to write zero-diff pause marker",
                        ticket.id,
                        exc_info=True,
                    )
                # Transition to AWAITING_USER_REPLY.
                ctx.service.transition(
                    ticket.id,
                    State.AWAITING_USER_REPLY,
                    note=_note,
                )
                updated = ctx.service.get(ticket.id)
                if updated:
                    from ...notify import send_notification

                    send_notification(
                        updated,
                        State.AWAITING_USER_REPLY,
                        "zero-diff early-abort — awaiting operator reply",
                        ctx.settings,
                    )
                log.info(
                    "%s: zero-diff early-abort — %d consecutive no-diff "
                    "passes; pausing with ask_user",
                    ticket.id,
                    zd_count,
                )
                return Outcome(State.AWAITING_USER_REPLY, _note)

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

    # --- All preflight guards passed: increment the spawn counter ---
    # Only genuine re-spawns (retry_attempt == 0) count; transient
    # infrastructure retries must not burn the ticket's spawn budget.
    # Write a durable in-flight marker so a process death / SIGTERM
    # after this point is detectable by the next preflight.
    if spawn_limit > 0:
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
            write_spawn_in_flight(ws.artifacts_dir, spawn_count, counted=True)
        else:
            write_spawn_in_flight(ws.artifacts_dir, spawn_count, counted=False)

    return None
