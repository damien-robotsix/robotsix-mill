"""Settings field mixin: merge stage settings.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _MergeSettings(BaseModel):
    # --- merge stage: auto-rebase of stale PRs ---
    # When True (default), the merge stage autonomously rebases PRs in
    # human_mr_approval and waiting_auto_merge when they become conflicting
    # or stale-behind-target.  Set False to disable autonomous rebase
    # globally (the REBASING path from IMPLEMENT_COMPLETE is unaffected).
    autonomous_rebase_enabled: bool = Field(
        description="When true, the merge stage autonomously rebases parked PRs that become conflicting.",
        default=True,
    )
    # When a PR in human_mr_approval becomes conflicting (other PRs merged to
    # the target branch), the merge stage invokes the rebase agent to
    # resolve conflicts automatically.  This is the max number of
    # rebase attempts per ticket before escalating to BLOCKED.
    rebase_max_attempts: int = Field(
        description="Maximum rebase attempts per ticket before escalating to BLOCKED.",
        default=3,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # When a PR parked in human_mr_approval becomes conflicting or behind
    # target, the merge stage normally falls through to IMPLEMENT_COMPLETE
    # (→ REBASING next poll).  This cooldown keeps it in HUMAN_MR_APPROVAL
    # for N hours after the last successful rebase, avoiding continuous
    # re-rebasing of PRs nobody has approved yet.  Set to 0 to disable
    # (always fall through, pre-existing behaviour).
    parked_rebase_cooldown_hours: int = Field(
        description="Hours to wait before re-rebasing a PR parked in human_mr_approval. Set to 0 to disable.",
        default=4,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # --- merge stage: auto-fix of failing remote CI ---
    # When a PR in human_mr_approval has failing CI checks, the merge stage
    # transitions to fixing_ci and invokes the ci-fix agent.  The agent is
    # ONE-SHOT: it fixes the failure and pushes, then reports DONE or FAILED.
    # It does NOT wait for CI verification — the external FIXING_CI ⇄
    # IMPLEMENT_COMPLETE polling loop handles iteration.

    # Multi-repo merge path only (MultiRepoCiFixMixin): maximum number of
    # wait_for_ci iterations per ticket before escalating to BLOCKED.  The
    # single-repo CIFixStage no longer uses this — its agent is one-shot.
    # Sized against observed runs, not guesswork: sampled successful ci_fix
    # stages completed in 300-900 s, all of them on a SINGLE verify iteration.
    # 3 leaves two retries for the "fix reveals the next failure" case while
    # keeping the derived stage ceiling (see Settings.stage_timeout_for) inside
    # what a 3-slot worker pool can afford to hold.
    #
    # A 3 -> 5 bump has been proposed twice and is deliberately NOT applied:
    # once as AC #3 of PR #2988 (lost in a rebase, so never shipped) and again
    # as PR #3018, reverted by #3019. Two reasons it was declined. First it is
    # inert where it matters: the deployed mill pins this key -- along with ~297
    # others -- in its stored config, so the shipped default is shadowed and
    # only the pin decides. Second the ceiling above is real: at 900 s per wait
    # plus the coordinator budget, 5 iterations take the ci_fix stage ceiling
    # from 3600 to 5400 s on shipped defaults (4800 -> 6600 s with the live
    # mill's pinned coordinator budget), i.e. a third of a 3-slot pool held for
    # 90 minutes. Raise the PIN, per-repo, if a specific board needs the extra
    # cascade room -- do not raise the fleet default.
    ci_fix_max_iterations: int = Field(
        description="Multi-repo merge path only: maximum wait_for_ci iterations per ticket before escalating to BLOCKED.",
        default=3,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Wall-clock timeout (seconds) for a single ci-fix agent pass.  This
    # wraps the LLM agent call inside the stage and fires BEFORE the
    # worker's stage timeout, so the stage produces a diagnostic block
    # note (failing check, last known state) instead of a bare timeout.
    # Set to 0 to disable (agent runs until the worker timeout or
    # request limit).
    #
    # This is a FLOOR, not the applied value.  Settings.
    # ci_fix_agent_timeout_effective raises it to at least
    # ci_fix_agent_budget_seconds (= ci_fix_max_iterations x
    # ci_fix_wait_timeout_s + the coordinator budget), and
    # Settings.stage_timeout_for keeps the stage wrapper above that
    # again — so raising the iteration budget can no longer leave the
    # agent unable to spend it.  Read those two before tuning this.
    ci_fix_agent_timeout_seconds: int = Field(
        description=(
            "Floor for the ci-fix agent pass wall-clock (seconds); raised to "
            "ci_fix_agent_budget_seconds when that is larger. 0 disables."
        ),
        default=1800,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Multi-repo merge path only (MultiRepoCiFixMixin): that path still runs
    # the legacy one-shot-per-cycle agent with an external retry loop, so it
    # keeps its own attempt + cycle ceilings.  The single-repo CIFixStage no
    # longer uses these — its budget is ci_fix_max_iterations.
    ci_fix_max_attempts: int = Field(
        description="Multi-repo merge path only: max ci-fix attempts per cycle.",
        default=2,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    ci_fix_max_cycles: int = Field(
        description="Multi-repo merge path only: max ci-fix cycles per ticket.",
        default=3,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Number of consecutive identical-failure cycles before escalating to
    # BLOCKED.  When the same CI failure fingerprint repeats this many times
    # without the ci-fix agent making progress, the stage short-circuits
    # instead of burning the agent's iteration budget on a fix that never
    # resolves.  Set to 0 to disable the check entirely.
    ci_fix_max_identical_failures: int = Field(
        description="Consecutive identical CI failure cycles before escalating. 0 disables.",
        default=2,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Number of consecutive identical merge-guard blocks before escalating
    # to a stronger BLOCKED that requires human intervention.  When the
    # deliver stage's meta-triage-fallback guard blocks with the same
    # fingerprint this many consecutive times without progress (e.g. the
    # same brand-new top-level file is detected each cycle), the stage
    # escalates instead of burning cost on a deterministic resume→block
    # loop.  Set to 0 to disable the check entirely.
    deliver_max_identical_blocks: int = Field(
        description="Consecutive identical merge-guard blocks before stronger escalation. 0 disables.",
        default=2,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Multi-repo merge path only: how often (seconds) wait_for_ci polls
    # the forge for the branch's CI conclusion while a run is in progress.
    ci_fix_wait_poll_interval_s: float = Field(
        description="Multi-repo merge path only: seconds between CI conclusion polls during wait_for_ci.",
        default=30.0,
        gt=0,
        json_schema_extra={"advanced": True},
    )

    # Multi-repo merge path only: maximum seconds a single wait_for_ci call
    # blocks before returning a still-pending signal.
    ci_fix_wait_timeout_s: float = Field(
        description="Multi-repo merge path only: maximum seconds a single wait_for_ci call blocks before returning still-pending.",
        default=900.0,
        gt=0,
        json_schema_extra={"advanced": True},
    )

    # Multi-repo merge path only: early bail-out when CI is stuck.
    ci_fix_max_consecutive_pending: int = Field(
        description=(
            "Multi-repo merge path only: early bail-out threshold — return CI_STUCK after this many "
            "consecutive CI_STILL_PENDING results. 0 disables."
        ),
        default=2,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Maximum number of automatic CI re-runs for transient/infrastructure
    # failures (network flakes, runner shutdowns, buildkit boot timeouts,
    # etc.) before escalating to a blocking ci_fix_dependency ticket.
    # Set to 0 to disable automatic transient re-runs entirely.
    ci_transient_max_retries: int = Field(
        description="Maximum automatic CI re-runs for transient failures before escalating. 0 disables.",
        default=3,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Per-run request budget for the ci-fix agent.  Must cover the agent's
    # entire fix→push pass (reads, edits, run_command, push), so it is
    # larger than a simple single-tool budget.  When exhausted,
    # pydantic-ai raises UsageLimitExceeded, which the retry layer catches and
    # triggers the fallback model (if configured).  Set to 0 to disable.
    ci_fix_request_limit: int = Field(
        description="Per-run request budget for the ci-fix agent. 0 disables.",
        default=120,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Maximum characters of inline job-log context in the failing summary
    # built for the ci-fix agent.  The forge already windows each job log
    # on the first failure marker at ``ci_log_max_bytes``; this caps the
    # TOTAL concatenated log so a hard ticket's repeated fix attempts stop
    # re-sending unbounded log history.  The agent can still expand on
    # demand via ``fetch_ci_logs(full_log=True)``.  0 disables the cap.
    ci_fix_log_context_max_chars: int = Field(
        description="Maximum characters of inline CI job-log context in the ci-fix failing summary. 0 disables.",
        default=16000,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Multi-repo merge path only: maximum characters of a COMPACT failure
    # summary returned by ``wait_for_ci`` on the 2nd and later iterations
    # of one ci-fix run.  The single-repo CIFixStage no longer uses this.
    # The first iteration (and the initial dispatch prompt) always receive
    # the full, already-capped failure detail; later iterations receive a
    # bounded digest (failing check names + first-error signatures + a
    # short job-log window) so the pydantic-ai conversation transcript
    # stops growing with loop depth — prior attempts' full logs and
    # annotations are not re-sent verbatim on every turn.  The agent can
    # still expand on demand via ``fetch_ci_logs``.  0 disables compacting
    # (every iteration sends the full summary — the pre-existing behaviour).
    ci_fix_iteration_summary_max_chars: int = Field(
        description="Maximum characters of the compact wait_for_ci failure summary for iterations >= 2. 0 disables compacting.",
        default=2000,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # The non-log portions of a failing summary (check annotations and the
    # code-scanning alert lists) are the other unbounded growth vector on
    # CodeQL-heavy failures — a run with hundreds of alerts/annotations
    # could otherwise blow up the prompt even with capped logs.  These two
    # caps bound the rendered annotation and alert lines per summary.
    # 0 disables the respective cap.
    ci_fix_max_annotations: int = Field(
        description="Maximum check annotations rendered per ci-fix failing summary. 0 disables.",
        default=40,
        ge=0,
        json_schema_extra={"advanced": True},
    )
    ci_fix_max_alerts: int = Field(
        description="Maximum code-scanning alert lines rendered per ci-fix failing summary. 0 disables.",
        default=40,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Per-ticket ceiling on how many times the worker may re-dispatch the
    # SAME LLM-bearing pipeline stage within a single processing pass before
    # pausing the ticket to BLOCKED for human review. Guards against an
    # unbounded implement↔review / implement↔ci_fix re-run loop (one ticket
    # spinning the model agent dozens of times and burning subscription
    # quota). Default 3 allows the initial run plus 2 re-runs.
    # Set to 0 to disable.
    #
    # This is a FLOOR, not the applied value: a review round that
    # requests changes re-dispatches implement, so
    # Settings.ticket_state_cycle_limit_effective raises it to at least
    # review_max_rounds + 1.  Without that the guard fired on tickets
    # that were merely using the review budget the pipeline granted them.
    ticket_state_cycle_limit: int = Field(
        description=(
            "Floor for the per-ticket same-LLM-stage dispatch ceiling within one "
            "processing pass; raised to review_max_rounds + 1. 0 disables."
        ),
        default=3,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # When True (default), ci_fix may invoke a conservative codeql_fp_triage
    # sub-agent at the hard cycle ceiling when the ONLY remaining red check
    # is CodeQL code-scanning.  The sub-agent evaluates alerts and may dismiss
    # high-conviction false positives, unblocking the ticket.  Set False to
    # disable this automatic unblock path.
    codeql_fp_triage_enabled: bool = Field(
        description="When true, ci_fix may invoke codeql_fp_triage to dismiss high-confidence CodeQL false positives.",
        default=True,
        json_schema_extra={"advanced": True},
    )

    # Cross-stage ceiling on combined REBASING + FIXING_CI dispatches without
    # CI turning green.  This counter spans both stages and is the universal
    # backstop: a ticket whose CI keeps failing enters REBASING or FIXING_CI
    # at most auto_fix_max_cycles times total, after which it is escalated to
    # BLOCKED without dispatching to either stage.  Reset only when CI is
    # observed green (the ONLY genuine forward-progress signal).  Set to 0 to
    # disable.  Default 6 (covers e.g. 3 rebase + 3 ci_fix cycles).
    auto_fix_max_cycles: int = Field(
        description="Cross-stage ceiling on combined REBASING + FIXING_CI dispatches without green CI. 0 disables.",
        default=6,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Ceiling on REBASING ↔ FIXING_CI alternations (ping-pong) before
    # escalating to BLOCKED.  A single alternation is a rebase→ci_fix or
    # ci_fix→rebase transition; the counter increments on each alternation.
    # When ping_pong_count reaches ping_pong_max_alternations, the next
    # alternation is blocked.  Reset when CI is observed green.  Set to 0
    # to disable.  Default 3.
    ping_pong_max_alternations: int = Field(
        description="Ceiling on REBASING to FIXING_CI alternations before escalating. 0 disables.",
        default=3,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Ceiling on consecutive merge polls where CI is fully green (nothing
    # pending) yet the forge still refuses to promote the PR.  That pairing is
    # normally a few seconds of settling, but it is *permanent* when a required
    # status context can never report — e.g. a job was renamed, so branch
    # protection waits for a context no workflow on this PR produces.  Without
    # a ceiling the merge stage re-polls until the worker's stage timeout kills
    # it and the ticket blocks with "stage merge timed out after Ns", which
    # names neither the PR nor the missing check.  Set to 0 to disable.
    green_unpromotable_max_polls: int = Field(
        description="Ceiling on consecutive polls with green CI but an unpromotable PR before escalating. 0 disables.",
        default=10,
        ge=0,
        json_schema_extra={"advanced": True},
    )

    # Maximum review-revision attempts per ticket before escalating to BLOCKED.
    review_revision_max_attempts: int = Field(
        description="Maximum review-revision attempts per ticket before escalating to BLOCKED.",
        default=2,
        ge=1,
        json_schema_extra={"advanced": True},
    )
