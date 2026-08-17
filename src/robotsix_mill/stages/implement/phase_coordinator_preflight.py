"""Preflight gate checks for the implement phase.

Extracted from :class:`PhaseCoordinatorMixin` to reduce file size.
All gate checks run BEFORE a Langfuse trace opens, catching
known-no-op conditions without consuming a spawn slot or emitting
a $0.00 trace.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone

from robotsix_mill._resources import (
    effective_language_instructions_dir,
    effective_skills_dir,
)

from ..._resources import agent_definitions_dir
from ...agents.runners.diagnostic_events import emit_diagnostic_event
from ...agents.yaml_loader import load_agent_definition
from ...config import effective_target_branch
from ...core.constants import NON_FEEDBACK_AUTHORS
from ...core.models import Comment, Ticket, TicketKind
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
    spawn_limit = s.implement_max_spawns_per_ticket
    counter_path = ws.artifacts_dir / "implement_spawn_count"
    if spawn_limit > 0:
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
                "Resume-blocked to retry: it clears the counter "
                "automatically (no workspace surgery needed)."
            )
            # Append the tail of the last implement summary so the
            # operator sees the genuine failure cause instead of only
            # the generic limit message.
            summary_tail_appended = False
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
                        summary_tail_appended = True
            # When no implement summary tail was appended, the spawns
            # likely aborted before any LLM work (e.g. a mill restart
            # burned the slot pre-trace).  Surface the durable spawn-
            # consumption breadcrumbs so the block note and the
            # SPAWN_LIMIT_EXHAUSTED diagnostic never lack evidence.
            if not summary_tail_appended:
                breadcrumbs_path = (
                    ws.artifacts_dir / "implement_spawn_breadcrumbs.log"
                )
                try:
                    breadcrumbs_text = breadcrumbs_path.read_text(
                        encoding="utf-8"
                    )
                except (OSError, UnicodeDecodeError):  # fmt: skip
                    breadcrumbs_text = ""
                if breadcrumbs_text:
                    crumb_tail = breadcrumbs_text[-800:].strip()
                    if crumb_tail:
                        note += (
                            "\n\nSpawn-consumption breadcrumbs (no "
                            "implement summary was produced — spawns "
                            "likely aborted before doing work):\n"
                            + crumb_tail
                        )
            # Emit a structured diagnostic event so agents
            # (including the periodic diagnostic agent) can
            # discover the exhaustion programmatically and
            # decide whether to request a counter reset or
            # file a deeper bug ticket.
            try:
                board_id = ctx.memory_board_id(ticket)
                normalized_key = (
                    "spawn_limit_exhausted:"
                    + hashlib.sha256(
                        f"{ticket.id}:{spawn_count}:{spawn_limit}".encode()
                    ).hexdigest()[:16]
                )
                emit_diagnostic_event(
                    ctx.settings,
                    board_id,
                    category="SPAWN_LIMIT_EXHAUSTED",
                    ticket_id=ticket.id,
                    reason=note,
                    normalized_key=normalized_key,
                )
            except Exception:
                log.exception(
                    "%s: failed to emit SPAWN_LIMIT_EXHAUSTED event",
                    ticket.id,
                )
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

    # 4.6. Changelog-only review re-spawn short-circuit: a ticket that
    #      came back from review with OPEN feedback threads and whose
    #      previous implement attempt committed only changelog
    #      fragments did NOT address that feedback — re-spawning would
    #      re-review the same diff and burn the remaining spawn budget
    #      (ticket b92d / faa8: three identical review verdicts, two
    #      changelog-only no-op attempts, 3/3 → BLOCKED with no
    #      progress).  Escalate to BLOCKED before the agent loop —
    #      without consuming a spawn — and surface the reviewer's open
    #      gap list (not the summary tail) in the block note.
    _changelog_guard = _changelog_only_review_respawn_guard(ticket, ctx)
    if _changelog_guard is not None:
        clear_conversation_state(ws, "implement")
        return _changelog_guard

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
    if spawn_limit > 0 and ticket.retry_attempt == 0:
        spawn_count += 1
        try:
            counter_path.write_text(str(spawn_count), encoding="utf-8")
        except OSError:
            log.warning(
                "%s: failed to write implement_spawn_count",
                ticket.id,
                exc_info=True,
            )
        # Durable breadcrumb: record that this spawn slot was
        # consumed so a spawn that aborts before any LLM work (e.g.
        # a mill restart) still leaves evidence for the eventual
        # SPAWN_LIMIT_EXHAUSTED block note.  Best-effort only — it
        # must never change control flow or raise.
        breadcrumb_path = ws.artifacts_dir / "implement_spawn_breadcrumbs.log"
        try:
            stamp = datetime.now(timezone.utc).isoformat()
            with breadcrumb_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{stamp} {ticket.id} spawn slot "
                    f"{spawn_count}/{spawn_limit} consumed — "
                    "trace about to open\n"
                )
        except OSError:
            log.warning(
                "%s: failed to append implement_spawn_breadcrumbs",
                ticket.id,
                exc_info=True,
            )

    return None


def _is_changelog_path(path: str) -> bool:
    """True for paths that only carry changelog prose.

    ``CHANGELOG.md`` counts alongside ``changelog.d/`` fragments: an
    attempt that rewrote the rendered changelog and nothing else is
    just as much a no-op against open review feedback.
    """
    return path.startswith("changelog.d/") or path == "CHANGELOG.md"


def _changelog_only_review_respawn_guard(
    ticket: Ticket,
    ctx: StageContext,
) -> Outcome | None:
    """Detect a changelog-only re-attempt with unaddressed review feedback.

    A review re-spawn whose branch carries only changelog fragment(s)
    while review threads remain open would re-review the same diff and
    burn the remaining spawn budget.  Returns the BLOCKED outcome for
    preflight to short-circuit with, or ``None`` when the guard does
    not apply.

    The verdict is taken over the WHOLE branch diff
    (``origin/<target>...HEAD``), not the tip commit.  Implement
    routinely lands its code in one commit and the changelog fragment
    in a follow-up, so a tip-commit check called every such attempt
    changelog-only and blocked tickets whose branch held a full
    implementation (ticket aadb: eight source files in HEAD~1, blocked
    on a HEAD that added one ``.feature.md``).

    Like the stale-spec guard above, the block is suppressed while a
    matching ``implement_changelog_override`` marker is present —
    written by ``_clear_stale_implement_guard`` on an operator
    ``resume-blocked`` with a justification note.  Without an escape
    hatch this guard was terminal: it fires from repo state alone, and
    the only thing that can change that state is the implement pass it
    refuses to spawn, so the ticket ping-pongs BLOCKED→READY→BLOCKED
    forever.  The marker holds the branch tip it was granted for, so
    one override buys exactly one fresh attempt — if that attempt again
    commits nothing but changelog, HEAD has moved and the guard re-arms.
    """
    ws = ctx.service.workspace(ticket)
    repo_dir = ws.dir / "repo"
    if ticket.review_rounds <= 0 or not (repo_dir / ".git").exists():
        return None

    comments: list[Comment] = []
    try:
        comments = ctx.service.list_comments(ticket.id)
    except Exception:
        return None
    open_feedback = [
        c
        for c in comments
        if c.parent_id is None
        and c.closed_at is None
        and c.author not in NON_FEEDBACK_AUTHORS
    ]
    if not open_feedback:
        return None

    target = effective_target_branch(ctx.settings, ctx.repo_config)
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        branch_files = [
            ln.strip()
            for ln in subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "diff",
                    "--name-only",
                    f"origin/{target}...HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.splitlines()
            if ln.strip()
        ]
    except (subprocess.SubprocessError, OSError):  # fmt: skip
        return None
    if not branch_files or not all(_is_changelog_path(f) for f in branch_files):
        return None

    override_path = ws.artifacts_dir / "implement_changelog_override"
    if head and override_path.exists():
        try:
            if override_path.read_text(encoding="utf-8").strip() == head:
                return None
        except OSError:
            pass

    gaps = "\n".join(f"- [review #{c.id}] {c.body.strip()}" for c in open_feedback)
    return Outcome(
        State.BLOCKED,
        "changelog-only re-attempt with unaddressed review "
        "feedback — the branch carries only changelog fragment(s) "
        "while review threads are still open; re-spawning would "
        "re-review the same diff without addressing the feedback.  "
        "Resume-blocked WITH a justification note to force one fresh "
        "attempt.  Unaddressed review feedback:\n" + gaps,
    )
