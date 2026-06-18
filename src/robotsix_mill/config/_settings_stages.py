"""Settings field mixin: web access, approval/review gates, retrospect, merge, CI monitor, langfuse cleanup.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class _StagesSettings(BaseModel):
    # --- agent web access (refine + implement) ---
    # Web search is delegated to a cheap, bounded SUB-agent: the main
    # (expensive) agent never carries OpenRouter's ":online" suffix, it
    # only gets a `web_research(query)` tool whose body runs this small
    # model — with ":online" + web_fetch — and returns just a concise
    # conclusion. This kills the per-request web-search surcharge on the
    # pricey model and keeps its context lean (conclusions, not pages).
    web_search: bool = Field(default=True)
    # web_research is a focused single-question summariser — it reads
    # a URL or two and returns one concise factual conclusion. Flash
    # is plenty for that distillation, and a v4-pro context bloated
    # by a 314KB raw web_fetch costs $0.25 per call (seen in trace
    # d40e3c9d4fa5add80b2fe313c1d821f2 — pipeline ticket f6e2 refine).
    # Flip the default to flash. Operators who want v4-pro back set
    # MILL_WEB_RESEARCH_MODEL.
    web_research_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    web_research_request_limit: int = Field(default=8, ge=1)
    # web_fetch runs in its OWN container: network ON, but NO repo/data
    # mount, non-root, read-only, fixed curl. Trade-off accepted: an
    # agent could encode data into a fetched URL. http(s) only.
    fetch_image: str = Field(default="curlimages/curl:8.17.0")
    web_fetch_max_bytes: int = Field(default=2_000_000, ge=0)
    web_fetch_timeout: int = Field(default=30, gt=0)
    # Post-extraction cap, applied AFTER HTML→text stripping. The
    # network-level ``web_fetch_max_bytes`` bounds raw bytes; this
    # bounds what the agent ACTUALLY sees in its context. Default
    # 200 KB ≈ 50K tokens — enough for one doc page worth of prose,
    # not enough to nuke a refine context with a 315 KB markup dump.
    # Configured via ``web.fetch_max_text_bytes`` in the YAML config.
    web_fetch_max_text_bytes: int = 40_000
    # When True, web_fetch returns the raw response body verbatim
    # (no HTML→text stripping, no per-run URL dedupe). Operator
    # escape hatch for the rare case the agent needs the markup
    # itself (parsing structure, inspecting attributes). Default
    # False — every agent we ship is a prose consumer.
    # Configured via ``web.fetch_raw`` in the YAML config.
    web_fetch_raw: bool = False
    # Bounded web-fetch budget, reset once per ``ask_web_knowledge``
    # consult and shared across every ``web_research`` sub-agent it
    # spawns. The ``*_request_limit`` knobs count MODEL requests, not
    # tool calls, so they can't bound fetch fan-out — a single consult
    # can issue dozens of ``web_fetch`` calls (7 searches × up-to-8
    # requests × multiple fetches → ~1.9M input tokens in one observed
    # refine specimen). These two caps bound that explosion directly.
    # Cache hits and ``web_fetch_raw`` returns do NOT count.
    # Max real (cache-miss) fetches per consult.
    # Configured via ``web.fetch_max_calls`` in the YAML config.
    web_fetch_max_calls: int = Field(default=15, ge=1)
    # Cumulative ceiling on returned (post-extraction, post-cap) text
    # bytes per consult; ``0`` disables the byte ceiling.
    # Configured via ``web.fetch_max_total_bytes`` in the YAML config.
    web_fetch_max_total_bytes: int = Field(default=2_000_000, ge=0)
    # Per-TRACE (cross-consult) web budget for the refine stage,
    # mirroring the proven survey caps. The per-consult ``web_fetch_max_*``
    # fields above bound a single ``ask_web_knowledge`` call; these bound
    # every fetch/search across one whole refine run, so a refine loop
    # can't re-bill millions of input tokens on runaway web I/O. Reset
    # once at the start of each refine trace (see ``run_refine_agent``).
    # Max real (cache-miss) fetches across one refine trace.
    refine_web_fetch_max_calls: int = Field(default=5, ge=1)
    # Max fetch bytes across one refine trace; ``0`` disables the ceiling.
    refine_web_fetch_max_total_bytes: int = Field(default=500_000, ge=0)
    # Max web_search calls across one refine trace.
    refine_web_search_max_calls: int = Field(default=5, ge=1)
    # Pre-write Python syntax check on `write_file` / `edit_file`. When
    # True (default) a SyntaxError aborts the edit and the agent gets
    # an actionable error string instead of writing broken code that
    # would only be caught one expensive test cycle later.
    # Configured via ``core.lint_on_edit`` in the YAML config.
    lint_on_edit: bool = Field(default=True, alias="MILL_LINT_ON_EDIT")
    # Character cap on an *implicit full* ``read_file`` (offset=1,
    # limit=None) payload, applied by ``fs_tools._bound_full_read``.
    # Over the cap the tool returns a head+tail slice plus an elision
    # marker that steers the agent to re-read the omitted region with
    # offset/limit; explicit ranged reads are never truncated. 50,000
    # chars ≈ 12.5K tokens — comfortably above ordinary hand-written
    # source modules (which are returned in full) so only large
    # generated/lock/baseline files (uv.lock ≈ 290 KB,
    # mypy-baseline.txt ≈ 121 KB) get trimmed before they bloat the
    # prefix that is re-billed on every later tool turn. 0 disables the
    # guard. Configured via ``core.read_file_max_chars`` in the YAML
    # config.
    read_file_max_chars: int = Field(default=50_000, ge=0)
    # Directory of skill docs (skills/<name>/SKILL.md) injected into the
    # refine + implement agents' system prompt. Relative to CWD (/app in
    # the container, repo root locally).
    skills_dir: Path = Field(default=Path("skills"))
    # Directory of per-language instruction Markdown snippets
    # (agent_definitions/language_instructions/<language>.md) injected
    # into the implement agent's system prompt. Relative to CWD (/app
    # in the container, repo root locally).
    language_instructions_dir: Path = Field(
        default=Path("agent_definitions/language_instructions"),
    )

    # --- human approval gate (refine -> implement) ---
    # When true (default), the refine stage transitions to
    # human_issue_approval instead of ready — a human must approve before
    # the implement stage kicks in. Set false for fully-autonomous mode.
    require_approval: bool = Field(default=True)

    # When true, a cheap conservative LLM call inspects the refined spec
    # after refinement.  If the change is "obviously safe" (cosmetic,
    # doc-only, single-file, no logic changes) the ticket skips the
    # human approval gate and goes straight to READY.  When false
    # (default), every gated ticket waits for a human click.
    auto_approve_enabled: bool = Field(default=False)
    # Model for the auto-approve triage call — must be fast and cheap.
    auto_approve_model: str = Field(default="deepseek/deepseek-v4-flash")

    # --- dual-model review gate (implement → deliver) ---
    # When true, the implement stage transitions to code_review instead of
    # deliverable. A dedicated review agent audits the diff blind before the
    # deliver stage pushes + opens the PR. Default False (opt-in).
    review_enabled: bool = Field(default=False)
    # When true (and review is enabled + the review agent marks the
    # change as auto-merge-eligible), the merge stage will attempt to
    # merge its own green PR via the forge API without waiting for a
    # human. Default False (opt-in).
    auto_merge_enabled: bool = Field(default=False)
    # When True (default), the single-repo auto-merge decision detects
    # pre-existing main-branch CI debt: if every workflow failing on the
    # PR head is ALSO failing on the merge target, the failure was not
    # introduced by this PR and the ticket is routed to BLOCKED instead
    # of cycling rebase/ci-fix retries. Safe-by-default — it only ever
    # fires when main is demonstrably red on the same workflow(s); the
    # flag exists so an operator can disable it.
    auto_merge_main_debt_detection_enabled: bool = Field(default=True)
    # When True, the board's ticket detail drawer renders description.md
    # below the comments with a collapsible "Hide" toggle (the frontend
    # reads ``gatesCache.comments_after_body``). Default False (opt-in).
    comments_after_body: bool = Field(default=False)
    # When True (and a human reviewer requests changes on the PR),
    # the merge stage will invoke the review-revision agent to
    # implement the requested changes automatically. Default False
    # (opt-in — this is a powerful autonomous capability).
    review_feedback_enabled: bool = Field(default=False)
    # When True (default), a cheap triage LLM call runs before the full
    # refine agent.  Drafts that are already precise, single-scoped, and
    # implementation-ready skip the full refine — saving cost & latency.
    # Set False to force full refine for all tickets without a deploy.
    refine_triage_enabled: bool = Field(default=True)
    # When True (default), a maintenance-triage check runs during refine
    # to detect operational-action drafts (create repo, fork repo,
    # investigate) and route them directly to the MAINTENANCE state,
    # bypassing the full refine→implement pipeline.
    maintenance_triage_enabled: bool = Field(default=True)
    # When True, a deterministic pre-refine gate verifies that file paths
    # and line ranges cited in the ticket draft still exist on the
    # working branch's HEAD.  When the cited evidence has gone stale
    # (upstream rewrite, sibling commit, or hallucinated finding) the
    # ticket is short-circuited to DONE before the expensive refine
    # agent runs.  Default False (opt-in).
    freshness_gate_enabled: bool = Field(default=False)
    # When True, an LLM-based pre-refine gate re-evaluates whether a
    # spawned follow-up/corrective draft's cited gap (e.g. "add doc
    # section X", "remove dependency Y") still exists on HEAD.  When the
    # gap was already resolved in place by a parallel/parent ticket the
    # ticket is short-circuited to DONE before the expensive refine
    # agent runs.  Default False (opt-in — auto-closing tickets is
    # risky).
    obsolescence_gate_enabled: bool = Field(default=False)
    # When True, a deterministic pre-implement gate verifies that
    # external symbol/import prerequisites the spec declares in a
    # ``## Prerequisites`` / ````prereq```` block are satisfiable in the
    # cloned repo's environment.  When a declared prerequisite is not yet
    # importable (e.g. an unmerged external port) the ticket is
    # short-circuited to BLOCKED before the expensive implement agent
    # runs — the work is still required once the upstream symbol lands.
    # Default True: this is a no-op for the common case (a spec that
    # declares no ``## Prerequisites`` block — ``run_prerequisite_check``
    # returns early with empty ``unmet``) and degrades gracefully (any
    # checker error → proceed, never blocks); it only ever blocks on an
    # explicitly declared, verifiably unmet directive.
    prerequisite_gate_enabled: bool = Field(default=True)
    # When True, the refine stage runs a post-refinement review pass that
    # strips verbose exploratory narrative from the spec, producing a
    # concise version while saving the verbose original as an artifact.
    # Defaults to False (opt-in) to avoid surprising behaviour changes.
    spec_review_enabled: bool = Field(default=False)
    # When True (default), a cheap scope-triage LLM call inspects
    # out-of-scope file changes before blocking the ticket. The agent
    # decides EXPAND (legitimate), REJECT (scope creep), or ESCALATE
    # (uncertain). Set False to restore immediate BLOCKED behaviour.
    scope_triage_enabled: bool = Field(default=True, alias="MILL_SCOPE_TRIAGE_ENABLED")
    # Model for the review agent. Defaults to the capable coordinator model.
    # Override to use a *different* model for a genuinely independent review
    # perspective (the dual-model benefit).
    review_model: str = Field(default="deepseek/deepseek-v4-pro")
    # Model for the review-revision agent. Defaults to the capable
    # coordinator model. Override to use a different model.
    review_revision_model: str = Field(default="deepseek/deepseek-v4-pro")
    # When True, the deliver stage generates a structured PR body
    # (Summary / Changes / Test Plan) from the implementation diff
    # via a cheap one-shot LLM call instead of pasting the raw spec.
    pr_summary_enabled: bool = Field(default=False, alias="MILL_PR_SUMMARY_ENABLED")
    # Model for the PR-summary generation call — must be cheap and
    # fast (one-shot, small diff, structured output).
    pr_summary_model: str = Field(
        default="deepseek/deepseek-v4-flash", alias="MILL_PR_SUMMARY_MODEL"
    )
    # When True, Forge.create_repo() is permitted to create repositories
    # via the forge API. Default False (opt-in) — the operator must
    # explicitly enable this and ensure the GitHub App installation has
    # the necessary repository-creation scope.
    enable_repo_creation: bool = Field(default=False)
    # Default visibility for newly created repositories.
    # "public" — repos are public unless the caller specifies private=True.
    # "private" — repos are private unless the caller specifies private=False.
    repo_visibility_default: Literal["public", "private"] = Field(
        default="public", alias="MILL_REPO_VISIBILITY_DEFAULT"
    )
    # When True, the merge stage deletes the per-ticket head branch on the
    # forge after a ticket merges to DONE. Default True — cleans up
    # mill/<id> branches automatically; set False to keep them.
    delete_branch_on_merge: bool = Field(default=True)
    # -- periodic stale-branch cleanup --
    # When True, the worker runs a periodic pass that lists remote branches
    # and deletes old, unprotected, no-open-PR branches (per prefix/age guards).
    # Default False — destructive, opt-in.
    stale_branch_cleanup_periodic: bool = Field(default=False)
    # Seconds between automatic stale-branch cleanup passes. Only used when
    # MILL_STALE_BRANCH_CLEANUP_PERIODIC=true. Enforced minimum 3600s (1h)
    # in the worker to avoid hammering the forge API.
    stale_branch_cleanup_interval_seconds: int = Field(default=86400)
    # A branch is eligible for cleanup only if its last commit is older than
    # this many days. Default 30 days.
    stale_branch_max_age_days: int = Field(default=30)
    # When True, only delete branches whose name starts with ``branch_prefix``
    # (the "old mill" branches). When False, also reap any other stale branch
    # ("stale dev").
    stale_branch_cleanup_prefix_only: bool = Field(default=True)
    # Maximum number of CODE_REVIEW → READY → DOCUMENTING → CODE_REVIEW
    # round-trips before escalating to DELIVERABLE for human merge approval.
    # A value ≤ 0 means escalate on the first REQUEST_CHANGES (the loop is
    # effectively disabled). Default 3.
    review_max_rounds: int = Field(default=3, ge=0)
    # How many model requests the review agent may make in one run
    # (counts each tool call + each reasoning step + the final verdict).
    # 20 (original) then 40 each routinely BLOCKED medium PRs with
    # "review agent error — resumable" mid-review. 40 was *still* too low:
    # test-heavy / multi-file diffs make the reviewer read_file the
    # source-under-test plus related modules to verify claims (the
    # preloaded reference_files cover only the modified files), and 40 was
    # exhausted on three tickets in one day (f0eb, 741f, 2e8d — each a
    # test-gap or multi-file change). Unlike refine, review has no explore
    # sub-agent to delegate breadth reads to, so the cap itself is the
    # lever; match refine's 80 (same reasoning: per-run cost is negligible
    # ~$0.03-0.09 and the per-ticket spend cap is the real backstop). See
    # ticket bc6d.
    review_request_limit: int = Field(default=80)
    # Maximum characters of the re-review prior-context block (prior
    # review comments + the implement rebuttal) fed to the review agent.
    # Each component is tail-kept (most-recent content survives) so multi-
    # round reviews don't re-pay for the entire accumulated history. 0
    # disables the cap.
    review_prior_context_max_chars: int = Field(default=8000, ge=0)
    # Maximum characters of the combined git diff injected into the review
    # prompt. The raw ``git diff origin/<target>...HEAD`` can balloon to
    # megabytes (divergent base, generated/lockfile churn, branch history)
    # regardless of how few lines the intended change touches, overflowing
    # even a 1M-token model context. Middle-truncated (head+tail) so both
    # early and late files get representation. ~200K chars ≈ 50K tokens
    # leaves ample room for spec + prior context + preseed + tools + the
    # output reservation. 0 disables the cap.
    review_diff_max_chars: int = Field(default=200_000, ge=0)
    # Output token budget for the review agent retry when the primary
    # attempt exhausts its max_tokens before generating a response
    # (the reasoning model burns output tokens on internal reasoning).
    # This is the *retry* budget; the primary attempt uses the YAML
    # max_tokens. Set higher than the YAML max_tokens. 0 disables the
    # output-exhaustion retry (falls straight to NEEDS_DISCUSSION).
    review_output_token_budget: int = Field(default=65536, ge=0)
    # How many model requests the scope-triage agent may make per
    # invocation (main call + any tool calls). Default 8: the agent is
    # tool-less, but structured-output retries (schema mismatch, output
    # retry) consume requests too — at 4 a single bad generation run
    # left zero headroom and the resulting "agent error" auto-escalated
    # tickets to humans (live case: ticket ff7f).
    scope_triage_request_limit: int = Field(default=8)
    # Maximum number of out-of-scope TEXT files fed into the scope-triage
    # prompt. When an implement pass leaves MORE than this many out-of-scope
    # text files (after binary-artifact auto-cleanup), treat it as a build-
    # artifact flood: skip the scope-triage LLM entirely (its prompt would
    # balloon to thousands of diff summaries) and block deterministically for
    # human review. Default 50 leaves normal PRs untouched. 0 disables.
    scope_triage_max_files: int = Field(
        default=50, ge=0, alias="MILL_SCOPE_TRIAGE_MAX_FILES"
    )
    # Per-call cap for pre-refine triage agent (main call + tool calls).
    triage_request_limit: int = Field(default=8)

    # Model for the documentation agent. Defaults to the capable
    # coordinator model.
    doc_model: str = Field(default="deepseek/deepseek-v4-flash")

    # --- retrospect stage (done -> reviewed) ---
    # When True, retrospect may file an improvement DRAFT. Until the
    # human-gate-after-refine exists, that draft auto-flows to done and
    # is retrospected again — set False to analyse without spawning.
    retrospect_spawn_drafts: bool = Field(default=True)
    # When True, retrospect may append AGENT.md proposals to
    # AGENT_CANDIDATES.md for human review.
    retrospect_spawn_agented_proposals: bool = Field(
        default=True, alias="MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS"
    )
    # (Removed) retrospect_deep_analysis_frequency: deep-analysis mode
    # was retired — per-trace inspection is now owned by the periodical
    # cost-evaluation pipeline (cost_reconciliation_runner +
    # trace_health_runner + expensive-item detector).
    # Model for the trace inspector sub-agent — a dedicated cheap model
    # that inspects a single trace's full observation tree.
    trace_inspector_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    # Model used by the periodic trace-review runner — a cheap-by-design
    # flash model so a 50-trace sweep doesn't burn the audit budget.
    trace_review_model: str = Field(
        default="deepseek/deepseek-v4-flash",
    )
    # Outlier thresholds for the deterministic trace-review classifier.
    # A trace is flagged for LLM inspection when ANY hit.
    #
    # Cost and observation count are flagged RELATIVELY: the runner
    # computes the median across the current batch and flags traces
    # whose value exceeds ``median × multiplier``. A multiplier of 3.0
    # means "3x the typical trace in this window." Batches with fewer
    # than 3 traces fall back to no relative flag (insufficient
    # baseline) — binary flags (tool errors, rejected generations,
    # ask_user loops, explore storms) still fire normally.
    trace_review_cost_multiplier: float = Field(
        default=3.0,
    )
    trace_review_obs_multiplier: float = Field(
        default=3.0,
    )
    # ``repeated_tool`` stays an absolute threshold because each tool
    # has its own "normal" usage profile — making it relative would
    # require a per-tool batch median, which is too noisy with small
    # samples.
    trace_review_max_repeated_tool: int = Field(
        default=50,
    )
    # Hard cap on the total number of tool calls the trace inspector
    # may make per trace.  100 tool calls is far beyond what any
    # legitimate trace analysis requires — only clearly broken runs
    # are terminated.  When exceeded, the inspector raises
    # ``UsageLimitExceeded`` and the trace is marked as errored.
    trace_review_max_tool_calls: int = Field(
        default=100,
    )
    # Hard cap on the number of tool-call errors before the trace
    # inspector is auto-terminated.  A healthy inspection should have
    # near-zero errors; 20 indicates a broken execution loop.
    trace_review_max_errors: int = Field(
        default=20,
    )
    # Hard cap on the total number of drafts a single trace-review
    # pass may file. The inspector emits one finding per flagged trace
    # and a typical batch flags 5-10 traces with 2-5 findings each →
    # up to 50 drafts per cycle (89 trace-review drafts piled up after
    # one 2026-05-28 cycle). Findings are individually low-signal and
    # the cross-trace analyzer is the right surface for recurring
    # patterns; capping per-cycle bleeds keeps the board readable.
    trace_review_max_drafts_per_run: int = Field(
        default=5,
    )
    # First-run lookback window when no watermark exists yet (hours).
    trace_review_initial_lookback_hours: int = Field(
        default=24,
    )
    # When set, every trace-review draft lands on THIS repo's board,
    # regardless of which repo the source trace lived on. Trace-review
    # findings are agent-side improvements (mill code, mill prompts);
    # filing them on each application repo's board scatters work that
    # belongs in one place. Leave empty to preserve the legacy
    # source-repo routing.
    trace_review_target_repo_id: str = Field(
        default="",
    )
    # Window (seconds) for correlating an incomplete trace with a
    # process restart. When an ``incomplete_trace`` flag fires AND
    # the trace's latest timestamp falls within this many seconds of
    # ``_process_started_at``, the ``restart_correlated`` flag is
    # appended — signalling the Phase 2 inspector that the root cause
    # is likely a restart kill, not an agent-loop bug.
    trace_review_restart_correlation_window_seconds: int = Field(
        default=60,
    )
    # Recency window (days) for the pre-filing duplicate check in the
    # trace-review runner.  A candidate prior ticket is considered for
    # dedup when its created_at is within this window of `now`.  Default
    # 7 mirrors `dedup_lookback_days` (used by the refine-stage dedup
    # guard) but is independent because the two checks live at different
    # stages and may want different policies.
    trace_review_dedup_lookback_days: int = Field(default=7)
    # Recency window (days) for the advisory pre-filing duplicate check in
    # epic decomposition (``dedup.find_child_overlaps``).  A proposed child
    # is flagged when a prior ticket created within this window matches its
    # scope.  Mirrors ``trace_review_dedup_lookback_days`` but is independent
    # so the epic-decomposition policy can diverge.
    epic_dedup_lookback_days: int = Field(default=7)
    # Memory ledger for the trace inspector.
    # Unset (default) derives <data_dir>/trace_inspector_memory.md.
    trace_inspector_memory_path: Path | None = Field(default=None)
    # Path to the agent-maintained Markdown memory ledger.  Override to
    # pin a specific path; unset (default) derives <data_dir>/retrospect_memory.md.
    retrospect_memory_path: Path | None = Field(default=None)
    # human_mr_approval (PR open) re-check cadence. mill has no scheduler; this
    # timer exists only to observe the external merge event.
    merge_poll_seconds: int = Field(default=120, gt=0)
    # When true (default), the workspace's clone (repo/) is removed on
    # close to save disk space.
    prune_clone_on_close: bool = Field(default=True)
    # Maximum number of terminal-state tickets (CLOSED, ANSWERED,
    # EPIC_CLOSED) to retain.  When a ticket transitions to a terminal
    # state and the total exceeds this cap, the oldest terminal tickets
    # (by created_at) are purged — unless they are the parent of an
    # active (non-terminal) child.  Set to 0 to disable purging.
    max_archived_tickets: int = Field(default=100, ge=0)
    # Maximum number of ProposedAction rows (all statuses) to retain.
    # When a new proposal is created and the total exceeds this cap,
    # the oldest terminal-status rows (REJECTED, EXECUTED, FAILED)
    # are purged.  PENDING and APPROVED rows are never purged by
    # this cap.  Set to 0 to disable purging.
    max_proposed_actions: int = Field(default=500, ge=0)

    # --- db maintenance (periodic archive purge + per-ticket event cap) ---
    # When True (default), the worker runs a periodic sweep that (a) purges
    # terminal tickets exceeding max_archived_tickets, (b) prunes oldest
    # TicketEvent rows on non-terminal tickets exceeding max_events_per_ticket,
    # and (c) runs PRAGMA optimize to reclaim freed pages.
    db_maintenance_periodic: bool = Field(default=True)
    db_maintenance_interval_seconds: int = Field(default=86400)
    # Maximum TicketEvent rows to retain per non-terminal ticket.
    # Events beyond this cap are pruned (oldest first).  Set to 0 to disable
    # per-ticket event capping entirely (archive purge still runs).
    max_events_per_ticket: int = Field(default=200, ge=0)

    # --- merge stage: auto-rebase of stale PRs ---
    # When a PR in human_mr_approval becomes conflicting (other PRs merged to
    # the target branch), the merge stage invokes the rebase agent to
    # resolve conflicts automatically.  This is the max number of
    # rebase attempts per ticket before escalating to BLOCKED.
    rebase_max_attempts: int = Field(default=3, ge=0)

    # --- merge stage: auto-fix of failing remote CI ---
    # When a PR in human_mr_approval has failing CI checks, the merge stage
    # transitions to fixing_ci and invokes the ci-fix agent.  The agent OWNS
    # the fix→push→verify loop: it fixes, pushes, and calls wait_for_ci to
    # block on the freshly-triggered CI run, iterating until CI is green or it
    # exhausts ci_fix_max_iterations verification attempts.  There is no
    # external FIXING_CI ⇄ IMPLEMENT_COMPLETE retry loop or per-ticket cycle
    # counter — the iteration budget lives inside the wait_for_ci tool.

    # Maximum number of times the agent may call wait_for_ci (i.e. push-and-
    # re-check iterations) for one ticket before it must report FAILED and the
    # stage escalates to BLOCKED.  Set to 0 only to effectively disable the
    # agent's verify loop (it would never be allowed to wait).
    ci_fix_max_iterations: int = Field(default=5, ge=0)

    # Multi-repo merge path only (MultiRepoCiFixMixin): that path still runs
    # the legacy one-shot-per-cycle agent with an external retry loop, so it
    # keeps its own attempt + cycle ceilings.  The single-repo CIFixStage no
    # longer uses these — its budget is ci_fix_max_iterations.
    ci_fix_max_attempts: int = Field(default=2, ge=0)
    ci_fix_max_cycles: int = Field(default=3, ge=0)

    # Number of consecutive identical-failure cycles before escalating to
    # BLOCKED.  When the same CI failure fingerprint repeats this many times
    # without the ci-fix agent making progress, the stage short-circuits
    # instead of burning the agent's iteration budget on a fix that never
    # resolves.  Set to 0 to disable the check entirely.
    ci_fix_max_identical_failures: int = Field(default=2, ge=0)

    # How often (seconds) wait_for_ci polls the forge for the branch's CI
    # conclusion while a run is in progress.
    ci_fix_wait_poll_interval_s: float = Field(default=30.0, gt=0)

    # Maximum seconds a single wait_for_ci call blocks before returning a
    # still-pending signal (the agent may then call it again).  Generous by
    # default because a full CI run (build + tests) can take many minutes.
    ci_fix_wait_timeout_s: float = Field(default=1500.0, gt=0)

    # Per-run request budget for the ci-fix agent.  Must cover ALL the agent's
    # fix→push→verify iterations (reads, edits, run_command, push, wait_for_ci),
    # so it is larger than the legacy per-cycle budget.  When exhausted,
    # pydantic-ai raises UsageLimitExceeded, which the retry layer catches and
    # triggers the fallback model (if configured).  Set to 0 to disable.
    ci_fix_request_limit: int = Field(default=120, ge=0)

    # When True (default), ci_fix may invoke a conservative codeql_fp_triage
    # sub-agent at the hard cycle ceiling when the ONLY remaining red check
    # is CodeQL code-scanning.  The sub-agent evaluates alerts and may dismiss
    # high-conviction false positives, unblocking the ticket.  Set False to
    # disable this automatic unblock path.
    codeql_fp_triage_enabled: bool = Field(default=True)

    # Cross-stage ceiling on combined REBASING + FIXING_CI dispatches without
    # CI turning green.  This counter spans both stages and is the universal
    # backstop: a ticket whose CI keeps failing enters REBASING or FIXING_CI
    # at most auto_fix_max_cycles times total, after which it is escalated to
    # BLOCKED without dispatching to either stage.  Reset only when CI is
    # observed green (the ONLY genuine forward-progress signal).  Set to 0 to
    # disable.  Default 6 (covers e.g. 3 rebase + 3 ci_fix cycles).
    auto_fix_max_cycles: int = Field(default=6, ge=0)

    # Ceiling on REBASING ↔ FIXING_CI alternations (ping-pong) before
    # escalating to BLOCKED.  A single alternation is a rebase→ci_fix or
    # ci_fix→rebase transition; the counter increments on each alternation.
    # When ping_pong_count reaches ping_pong_max_alternations, the next
    # alternation is blocked.  Reset when CI is observed green.  Set to 0
    # to disable.  Default 3.
    ping_pong_max_alternations: int = Field(default=3, ge=0)

    # Maximum review-revision attempts per ticket before escalating to BLOCKED.
    review_revision_max_attempts: int = Field(default=2, ge=1)

    # --- target-branch CI monitor ---
    # CI monitor enabled/interval are now per-repo fields on RepoConfig
    # (see config/repos.yaml).  ci_log_max_bytes stays global — it is an
    # operational cap, not a per-repo policy decision.
    ci_log_max_bytes: int = Field(default=65536)

    # --- langfuse cleanup (caps trace count per project) ---
    # When True, the worker runs a periodic sweep that deletes the oldest
    # traces from each repo's Langfuse project, keeping at most
    # langfuse_cleanup_max_traces rows. Default False (opt-in).
    langfuse_cleanup_periodic: bool = Field(default=True)
    langfuse_cleanup_interval_seconds: int = Field(default=86400)
    langfuse_cleanup_max_traces: int = Field(default=1000)
