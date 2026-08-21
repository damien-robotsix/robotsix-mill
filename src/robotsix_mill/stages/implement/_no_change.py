"""No-change contradiction detection for the implement stage.

Extracted from :mod:`.implementation_logic` to keep that module focused.
Contains the standalone ``_run_no_change_contradiction_check`` function
(formerly ``ImplementationLogicMixin._detect_no_change_contradiction``),
which routes an empty-diff run to BLOCK (lost work) or DONE (legitimate
no-op) based on edit-claim, gitignored-edit, and formatter-reverted
signals.

The function is a plain module-level function; it receives the mixin
class as its first argument (``cls``) so it can delegate back to the
mixin helpers it depends on (``cls._finalize``,
``cls._any_repo_has_changes``, ``cls._edits_formatter_reverted``,
``cls._claimed_gitignored_edits``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ...config import Settings
from ...core.models import Ticket
from ...core.states import State
from ...core.text_noop import spec_demands_code_change
from ..base import Outcome, StageContext
from ._shared import _ImplementContext, _SinglePassResult, log


def _run_no_change_contradiction_check(
    cls: Any,
    ctx: StageContext,
    ticket: Ticket,
    repo_dir: Path,
    branch: str,
    settings: Settings,
    summary: str,
    spec_text: str,
    ref_files: list[str] | None,
    new_msgs: bytes | None,
    no_change_needed: bool,
    no_change_rationale: str,
    resuming: bool,
    target: str,
    extra_roots: list[Path] | None,
    *,
    short_circuit_verify: Any,
    git_ops: Any,
    target_branch_for: Any,
    attempt: int = 1,
    max_iters: int = 1,
    new_ic: _ImplementContext | None = None,
) -> _SinglePassResult | None:
    """Check for edit-claim contradictions when the diff is empty.

    An agent that invokes file-mutating tools yet claims
    ``no_change_needed`` (or produces an empty diff on a fresh
    run) is signalling lost work — BLOCK for inspection instead
    of closing DONE.  A confirmed formatter-reverted / redundant
    edit is exempt (a true no-op).

    Returns a ``_SinglePassResult`` when a contradiction is found
    (caller must return it immediately); ``None`` when the empty
    diff is legitimate and the caller should close DONE.
    """
    _ = settings  # unused in this helper
    if not cls._any_repo_has_changes(repo_dir, extra_roots, target, settings=settings):
        if no_change_needed and no_change_rationale.strip():
            # Agent explicitly signalled no_change_needed.
            edit_tools = short_circuit_verify.detect_edit_claim_contradiction(
                has_changes=False, new_messages=new_msgs
            )
            if edit_tools:
                fmt_result = cls._edits_formatter_reverted(repo_dir, new_msgs)
                if fmt_result is not True:
                    tool_list = ", ".join(edit_tools)
                    diag = (
                        f"{no_change_rationale.strip() or summary}\n\n"
                        "[Diagnostic] implement was about to close this ticket "
                        "as ``no_change_needed`` because ``git diff`` is empty "
                        f"— but the agent invoked file-mutating tools "
                        f"({tool_list}) during the run, and replaying those "
                        "edits + formatting still produced a real change (or "
                        "could not be verified). An empty diff after real edit "
                        "calls means the work did NOT persist (edits reverted, "
                        "workspace reset mid-run, or written outside the clone). "
                        "Closing as no-change would silently lose that work, so "
                        "the ticket is BLOCKED for inspection. Re-run implement; "
                        "if the spec genuinely needs no change, the agent must "
                        "reach that conclusion WITHOUT calling "
                        "write_file/edit_file/Write/Edit."
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        diag,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            "edit-claim contradiction (empty diff after edit calls)",
                        ),
                    )
            # Guard: spec demands a non-empty diff — closing DONE
            # would contradict the spec.
            if spec_demands_code_change(spec_text):
                diag = (
                    f"{no_change_rationale.strip()}\n\n"
                    "[Diagnostic] The spec explicitly mandates a non-empty diff "
                    "but the agent concluded no_change_needed with an empty diff. "
                    "The spec demands code changes that do not exist at HEAD; "
                    "closing as DONE would be a false positive."
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    diag,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        "spec demands code change but diff is empty (no_change_needed)",
                    ),
                )
            # No contradiction — close DONE.
            rationale = no_change_rationale.strip()
            short = rationale[:400] + ("…" if len(rationale) > 400 else "")
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"no change needed — {rationale}",
                ok=True,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(State.DONE, f"no change needed — {short}"),
            )
        if not resuming:
            # Empty diff on a fresh run: the working tree is clean AND
            # the branch has no commits beyond ``origin/<target>`` —
            # there is genuinely nothing to merge.
            #
            # Two guards protect against silently closing real work:
            #   (a) gitignored edits — real writes into a gitignored
            #       path are invisible to ``git status`` and surface
            #       as an opaque empty diff. Closing DONE would lose
            #       deliverable work → BLOCK.
            #   (b) edit-claim contradiction — the run invoked
            #       file-mutating tools yet nothing landed (edits
            #       reverted, workspace reset mid-run, or written off
            #       clone). Closing DONE would lose that work → BLOCK.
            #       A confirmed formatter-reverted / redundant edit
            #       (``_edits_formatter_reverted`` is True) is a true
            #       no-op and is exempt.
            no_change_summary = summary or (
                "Agent finished without producing any file edits and "
                "without explanation. Check artifacts/implement_messages.json "
                "for the full transcript."
            )
            # Guard (a): gitignored-edit detector.
            ignored_hits = cls._claimed_gitignored_edits(repo_dir, new_msgs)
            if ignored_hits:
                hit_list = ", ".join(f"`{p}`" for p in ignored_hits)
                no_change_summary = (
                    f"edits landed in gitignored path(s): {hit_list} — the "
                    "files exist on disk but git cannot see them, so this "
                    "board cannot deliver them (vcs-imported / vendored "
                    "sub-tree). The spec must target git-tracked files, or "
                    "the board needs manifest-aware delivery for that "
                    f"sub-tree.\n\n{no_change_summary}"
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    no_change_summary,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                reason = " ".join(no_change_summary.split())
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        f"no changes produced — {reason[:300]}"
                        + ("… (see implement.md)" if len(reason) > 300 else ""),
                    ),
                )
            # Guard (b): edit-claim contradiction.
            edit_tools = short_circuit_verify.detect_edit_claim_contradiction(
                has_changes=False, new_messages=new_msgs
            )
            if edit_tools and (
                cls._edits_formatter_reverted(repo_dir, new_msgs) is not True
            ):
                tool_list = ", ".join(edit_tools)
                diag = (
                    f"{no_change_summary}\n\n"
                    "[Diagnostic] implement produced an empty diff, but the "
                    f"agent invoked file-mutating tools ({tool_list}) during "
                    "the run and replaying those edits + formatting still "
                    "produced a real change (or could not be verified). An "
                    "empty diff after real edit calls means the work did NOT "
                    "persist (edits reverted, workspace reset mid-run, or "
                    "written outside the clone). Closing as no-change would "
                    "silently lose that work, so the ticket is BLOCKED for "
                    "inspection."
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    diag,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        "edit-claim contradiction (empty diff after edit calls)",
                    ),
                )
            # Guard: spec demands a non-empty diff — closing DONE
            # would contradict the spec.
            if spec_demands_code_change(spec_text):
                diag = (
                    f"{no_change_summary}\n\n"
                    "[Diagnostic] The spec explicitly mandates a non-empty diff, "
                    "but implement produced an empty diff on a fresh run with no "
                    "lost-work evidence. The spec demands code changes that do "
                    "not exist at HEAD; closing as DONE would be a false positive."
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    diag,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        "spec demands code change but diff is empty (fresh run)",
                    ),
                )
            # Genuine no-op: clean working tree, no commits beyond the
            # base, no gitignored writes, no lost edits. The spec is
            # already satisfied — terminate DONE instead of looping.
            done_note = "already satisfied — no changes needed (empty diff vs base)"
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"{done_note}\n\n{no_change_summary}",
                ok=True,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            log.info(
                "%s: empty diff on fresh run with no lost work — DONE "
                "(already satisfied)",
                ticket.id,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(State.DONE, done_note),
            )
        # Resuming with empty diff: the branch already satisfied the
        # spec in a prior session and the current pass contributed
        # no new edits.  Route to DONE — just like the fresh-run
        # path above — instead of falling through to
        # _verify_repo_changes (which would block on zero tool calls
        # or route to CODE_REVIEW with an empty diff that delivers
        # nothing).
        if resuming:
            # Apply the same edit-claim contradiction guard as the
            # fresh-run path — an agent that called edit tools is
            # signalling lost work.
            edit_tools_rs = short_circuit_verify.detect_edit_claim_contradiction(
                has_changes=False, new_messages=new_msgs
            )
            if edit_tools_rs and (
                cls._edits_formatter_reverted(repo_dir, new_msgs) is not True
            ):
                tool_list = ", ".join(edit_tools_rs)
                diag = (
                    f"{summary or 'Agent finished without producing file edits.'}\n\n"
                    "[Diagnostic] implement produced an empty diff on a resuming "
                    f"run, but the agent invoked file-mutating tools ({tool_list}) "
                    "and replaying those edits + formatting still produced a real "
                    "change (or could not be verified).  Blocking for inspection."
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    diag,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        "edit-claim contradiction "
                        "(empty diff after edit calls on resume)",
                    ),
                )
            # Guard: spec demands a non-empty diff — closing DONE
            # would contradict the spec.
            if spec_demands_code_change(spec_text):
                diag = (
                    f"{summary or 'Agent found no work to do.'}\n\n"
                    "[Diagnostic] The spec explicitly mandates a non-empty diff, "
                    "but implement produced an empty diff on a resuming run. "
                    "The spec demands code changes that do not exist at HEAD; "
                    "closing as DONE would be a false positive."
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    diag,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        "spec demands code change but diff is empty (resume)",
                    ),
                )
            resume_done_note = (
                "already satisfied — no changes needed "
                "(resuming with empty diff vs base)"
            )
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"{resume_done_note}\n\n{summary or 'Agent found no work to do.'}",
                ok=True,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            log.info(
                "%s: empty diff on resuming run with no lost work — "
                "DONE (already satisfied)",
                ticket.id,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(State.DONE, resume_done_note),
            )
    # Resuming with a clean working tree but branch already ahead
    # of target: prior passes landed the implementation and the
    # current pass found nothing more to do.  Route to DONE instead
    # of falling through to CODE_REVIEW (which would re-review the
    # same prior work and loop back, burning spawn budget).
    _working_tree_clean = not git_ops.has_changes(repo_dir)
    if _working_tree_clean and extra_roots:
        for _rp in extra_roots:
            if _rp != repo_dir and git_ops.has_changes(_rp):
                _working_tree_clean = False
                break
    if resuming and _working_tree_clean:
        # Edit-claim contradiction guard: an agent that called
        # edit tools on a resume-with-ahead branch is signalling
        # lost work — those edits didn't land in the working tree.
        edit_tools_rs = short_circuit_verify.detect_edit_claim_contradiction(
            has_changes=False, new_messages=new_msgs
        )
        # ...unless every file it touched is ALREADY changed on the branch.
        # Then the edits were an idempotent re-application of work a prior
        # pass committed, which is the normal shape of a resume, not a
        # loss. Blocking those stranded 7 tickets across five boards
        # (observed 2026-08-02..08-07), five of them on this exact path.
        _target = target_branch_for(ctx.settings, ctx.repo_config)
        _branch_changed = git_ops.changed_source_files(repo_dir, _target)
        _already_landed = short_circuit_verify.claimed_edits_already_on_branch(
            new_messages=new_msgs,
            branch_changed_files=_branch_changed,
        )
        if _already_landed and edit_tools_rs:
            log.info(
                "%s: edit-claim contradiction suppressed — every claimed "
                "edit (%s) is already present in the branch's diff vs %s "
                "(idempotent re-application on resume)",
                ticket.id,
                ", ".join(edit_tools_rs),
                _target,
            )
        # A branch that already carries committed work is not "lost work",
        # even when one claimed path is missing from its diff.  The guard
        # below asks whether EVERY claimed edit landed; a single stray path
        # — a changelog fragment a later pass renamed away, a file the agent
        # wrote and then reverted — flipped that to False and blocked the
        # whole ticket.  Measured on 2026-08-13: four of the five tickets
        # blocked on this path had 2-4 commits and 2-6 changed files sitting
        # in their workspace, stranded (robotsix-chat a78d/a801/3f3e,
        # robotsix-mill 8b2c).  The fifth (959e) had an empty branch, which
        # is the shape the guard is actually for — so gate on that instead
        # and let a branch with real commits flow on to CODE_REVIEW →
        # deliver, exactly as the fall-through below already intends.
        # ``changed_source_files`` returns [] on any git error, so an
        # unreadable branch still blocks: the guard fails closed as before.
        if edit_tools_rs and not _already_landed and _branch_changed:
            # Name the paths that failed the all()-check, so the next
            # reader sees which edit went missing rather than only that
            # one did. ``detect_missing_claimed_files`` is deliberately
            # narrower (it reports only files the summary asserts as
            # landed), so it is the wrong lens for this log line.
            _on_branch = {os.path.basename(f) for f in _branch_changed if f}
            _absent = sorted(
                base
                for base in short_circuit_verify.run_claimed_edited_paths(new_msgs)
                if base not in _on_branch
            )
            log.warning(
                "%s: claimed edit(s) absent from the branch diff vs %s (%s), "
                "but the branch carries %d changed file(s) across real "
                "commits — proceeding to review rather than blocking",
                ticket.id,
                _target,
                ", ".join(_absent) or "none",
                len(_branch_changed),
            )
        if (
            edit_tools_rs
            and not _already_landed
            and not _branch_changed
            and (cls._edits_formatter_reverted(repo_dir, new_msgs) is not True)
        ):
            tool_list = ", ".join(edit_tools_rs)
            diag = (
                f"{summary or 'Agent finished without producing file edits.'}\n\n"
                "[Diagnostic] implement produced no new working-tree changes "
                "on a resuming run that already has prior commits, but the "
                f"agent invoked file-mutating tools ({tool_list}) and "
                "replaying those edits + formatting still produced a real "
                "change (or could not be verified).  Blocking for inspection."
            )
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                diag,
                ok=False,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(
                    State.BLOCKED,
                    "edit-claim contradiction "
                    "(empty diff after edit calls on resume-with-ahead)",
                ),
            )
        # Branch is ahead of target with a clean working tree on a
        # resuming run — prior passes already committed the work.
        # Instead of short-circuiting to DONE (which strands the
        # WIP commits — they never reach deliver, no PR is opened),
        # return None to let the normal flow proceed through
        # _verify_repo_changes → CODE_REVIEW → deliver. The deliver
        # stage will detect the ahead-of-target commits, push, and
        # create the PR.
        log.info(
            "%s: clean working tree on resuming run with branch ahead — "
            "proceeding to deliver (not DONE; WIP commits need to land)",
            ticket.id,
        )
        return None
    return None
