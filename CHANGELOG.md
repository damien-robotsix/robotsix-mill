## 0.0.0 (unreleased)

- Fix pipeline-wide agent-run crash: bump `robotsix-llmio` pin past the
  sync-wrapper fix (sync `call_with_retry`/`run_agent` invoked the caller's
  `run_sync`-style fn inside `asyncio.run()`, breaking every draft-refine and
  triage call), and add a running-loop guard to mill's `run_agent` mirroring
  #2451: when called with an event loop running (e.g. on the Claude SDK's
  loop), the retry session is delegated to a thread so `run_sync` can create
  its own loop.
- Fix redraft/re-block loop for tickets with existing all-green branches: implement stage now detects when a remote branch has green CI but no open PR and routes to IMPLEMENT_COMPLETE so the deliver stage re-opens the PR instead of re-running the implement loop. Also add a guard ensuring every BLOCKED transition records a reason in the history event.
- Add deploy-freshness gate to prevent wasted implement attempts on stale worker images. The implement preflight and resume-blocked paths now check ``GET /services/mill`` (when ``deploy_api_url`` is configured) and park tickets with an explicit "awaiting redeploy" note when the running image predates the latest digest.
- Add `state_sync` to the periodic-agent lists in `docs/agents/agent-yaml-schema.md` (category field reference and `read_ticket` field reference).
- Update `docs/agents/agent-yaml-schema.md`: replace stale `board` skill references with the three actual skills (`ask_user_guardrails`, `board-read`, `board-report`) and reflect `refine.yaml`'s real `skills` list
- Implement stage now bootstraps empty remote repos (no commits, no branches) with an initial README commit instead of blocking the ticket. Ports the cd2c pattern from the periodic meta agent's `clone_all_repos` path.
- Correct stale `modules: true` opt-in claim in `AGENT.md`: `refine.yaml` has opted in, `meta.yaml` explicitly sets `modules: false`.
- Remove dead `.src-security-posture` CSS rule from board-mill.css (no matching SourceKind enum member exists)
- `human_mr_approval`: discard stale `REQUEST_CHANGES` reviews when the PR head has changed since the review was cast (compare `review.commit_id` against `pr.head.sha`). Prevents the verified 7-cycle verdict-replay loop that dominated tickets where the diff issue was externally remediated.
- Fix `language_instructions_dir` default to resolve via `importlib.resources` instead of a bare relative `Path`, so the built-in language snippets are found in installed (container) mode. Add a preflight check that hard-blocks when the directory is absent, catching container-only path-resolution gaps before a model pass opens.
- Fix: meta-ticket workspace setup crashes on freshly-created empty repos — `build_meta_workspace` now detects empty remotes and bootstraps them with an initial commit, matching the existing `clone_all_repos` behaviour.
- Implement stage preflight now hard-blocks (instead of silently degrading) when the agent definition has no tools, a referenced skill file is missing, or the workspace directory is inaccessible. Each failure includes the specific path/condition in the error note, preventing the zero-tool-call no-op loop seen on non-mill boards.
- Bump `robotsix-llmio` git pin past 2026-07-16 to pick up
  optional-`RunContext` fix (`_tool_converter` now accepts tools
  with `ctx: RunContext[None] = None`, resolving a Claude SDK
  `takes_ctx=True` block on `read_file` and similar tools).
- Clear stale review artifact and stage-outcome cache on successful rebase so the review gate re-evaluates the current diff instead of replaying a cached REQUEST_CHANGES verdict. Also invalidate the review cache when the auto-merge eligibility gate detects a stale (head-SHA-mismatched) verdict.
- Fix zero-edit implement loop persisting after prior fix: the `reprompt_if_unstructed` guard now checks for zero tool calls BEFORE the `isinstance(expected_type)` short-circuit, so structured `ImplementResult` envelopes with no tool calls trigger a re-prompt (unless `no_change_needed=True`). Also, the per-pass stuck-loop detection in `_implement_loop` now computes `progress` regardless of `has_diff`, so leftover changes from a prior session cannot mask a current pass that contributed zero tool calls.
- Survey periodic pass now classifies findings as repo-specific or fleet-wide convention candidates, and can file companion tickets on the ``robotsix-standards`` board for generalizable conventions. The runner supports cross-board ticket creation via the new ``draft_target_repo_ids`` field on ``PeriodicAgentResult``, with creation-time dedup on the target board to prevent duplicate standards proposals across repos.
- Add zero-tool-call guard in implement stage: a pass where the agent issues no tool calls and produces no diff now surfaces a distinct BLOCKED error immediately in both the retry loop and the resume→CODE_REVIEW path, rather than masquerading as a generic no-edit stall. (mill: Implement agent spins with zero tool calls / zero edits on robotsix-auto-mail workspace (20260718T152204Z-implement-agent-spins-with-zero-tool-cal-1620))
- Resolve six trivial `# type: ignore` suppressions with proper type annotations, shrinking the mypy baseline by 4 entries (708→704).
- `state_sync` is now a mill-internal periodic agent (kind `mill_only`) — it no longer appears as an opt-in presence-file pass for managed repos. The agent continues to run against robotsix-mill on its existing schedule via the periodic supervisor's mill-repo guard.
- Add `verify_diff` tool to the implement agent: replaces 3-5 `run_command` grep/awk verification calls per `edit_file` with a single `git diff --stat` call plus optional expected-file cross-check. Registered in `ToolRegistry` category `git` and steered by a new "Batch verification with `verify_diff`" prompt section.
- Add module-level docstrings to `runtime/worker/processing.py` and `runtime/worker/epic.py`, matching the style of the other worker submodules.
- `resume-blocked` now only resets the implement spawn counter when the ticket was actually blocked at the spawn limit (counter ≥ `implement_max_spawns_per_ticket`), and records the reset as a history event ("spawn counter reset via resume-blocked"). Tickets blocked from READY for other reasons keep their counter intact.
- Add tiered test-run policy to implement agent prompt: targeted tests first, broader related tests second, never escalate to full suite (pipeline job).
- Add batching discipline rule to implement agent prompt: batch `git grep` / `run_command` questions into a single `explore` or `parallel_explore` call to reduce round-trips and wall-clock cost.
- Add `changelog_autofill_periodic` and `changelog_autofill_interval_seconds` settings fields, giving the changelog-autofill schedule-only pass a configurable kill-switch and interval (previously hardcoded to 86400 s with no disable option).
- GitLab forge: implement cross-project merge request support via `target_project_id` when `head_repo` is provided, matching the GitHub adapter's cross-fork PR workflow. Remove the `NotImplementedError` stub and the `_validate_cross_repo_forge_compat` guard that rejected `cross_repo_target` for GitLab.
- Document stage: deterministic short-circuit for doc-only diffs (all paths are `.md` or under `docs/`), skipping the classifier + doc agent and saving $0.005–0.01 per occurrence.
- Add class-level docstring to `PeriodicPassesMixin` describing its per-repo periodic pass orchestration.
- Added docstring to ``health_ready`` endpoint in ``_health.py`` documenting the readiness probe's Args, Returns shape, and 503-on-failure behaviour.
- Add docstring to `WorkerPool.start()` method in `src/robotsix_mill/runtime/worker/core.py`.
- Merge gate: stale review verdicts no longer block auto-merge after a rebase. The review artifact now records the branch head SHA; when the current PR head differs the stale verdict is ignored. Prevents the merge gate from re-posting byte-identical REQUEST_CHANGES verdicts that no longer apply to the rebased branch.
- Document `sandbox.image` dev-vs-prod dual default: the Pydantic model default is `python:3.14-slim` for lightweight local development, while the production JSON config overrides to `robotsix/mill-sandbox:latest` (includes `uv` and toolchain). Added inline docstring comment and updated config docs table to match.
- Fix orphan `agent run` Langfuse traces: propagate OTel/contextvars across
  `ThreadPoolExecutor` boundaries in watchdog/timeout helpers so pydantic-ai
  agent spans nest under the stage-named root trace instead of creating
  unattributed root spans.
- Update `docs/agents/agent-yaml-schema.md` to match the current `AgentDefinition` model: replace `model` with `level`, replace `web` with `web_knowledge`, add missing field docs (`list_epic_children`, `list_threads`, `ask_user`, `inject_agent_md`, `inject_language_conventions`, `max_tokens`), update category listings and tools table, fix `read_ticket` section.
- Fix `coordinator_timeout_seconds` model default drift: changed from 900 to 600 in `_settings_core.py` to match `config/config.example.json` and documentation.
- Fixed typo `rebasin` → `rebasing` in the valid State values list in the chat-skill endpoint docstring.
- Save conversation state on `AgentBudgetError` (budget exhaustion) so
  the implement agent can resume from where it left off instead of
  restarting from scratch. The BLOCKED→READY resume path now loads
  saved conversation state alongside `previous_attempt_summary`.
- Auto-generate board passes dropdown from the pass registry; remove hand-wired routes and buttons for trace_health, langfuse_cleanup, meta, and run_health — all passes now trigger via the generic ``POST /passes/{pass_id}/run`` endpoint.  Passes are grouped by kind (LLM Agents, Runners, Global) in the dropdown.
- **Board UI**: replaced hand-wired "Agents" dropdown with a dynamically-populated "Passes" dropdown driven by the periodic pass registry (`GET /passes` + `POST /passes/{pass_id}/run`). Passes are grouped by kind (LLM Agents / Runners). Adding a new pass to `_PASS_REGISTRY` is now the only wiring needed to make it manually triggerable from the board.
- Sync `STATE_TRACE` in `board-mill.js` with the canonical `STAGE_FOR_STATE` mapping from `states.py`: corrected `ready`→`"implement"`, `implement_complete`→`"merge"`, `rebasing`→`"merge"`, `done`→`"retrospect"`; added missing `draft: "refine"`; removed terminal `closed` (no stage).
- Extract shared standards-awareness prompt block into `agent_definitions/_shared/standards-awareness.yaml` and add `!include` resolution to `yaml_loader.py`. Survey and audit agents now consume the canonical block via `!include` instead of maintaining separate copies.
- Route small mechanical refactors (≤40 lines, no new files) to level-1 review model, reducing review cost for fully-prescribed extraction/move tickets by ~10×.
- Remove deprecated `env_doc_sync` periodic agent (agent definition, implementation module, route, CLI, board UI, config settings, and all test coverage). Env-var documentation consistency is now governed by robotsix-standards policy with audit enforcement.
- Remove the `security_posture` periodic agent entirely: delete the agent definition, source module, tests, runner config, CLI entry, HTTP route, board UI button, settings fields, and all code/docs references. Security posture is being codified in robotsix-standards as an auditable standard.
- New periodic agent `docstring_coverage`: scans Python source modules for public functions, classes, and methods with zero docstring, prioritizes by complexity, and files draft tickets. Includes YAML definition, Python module, presence file, SourceKind entry, settings, periodic-runner registration, CLI/API/board-UI wiring, and test suite.
- Add module-level docstring to `src/robotsix_mill/dev_tooling/__init__.py`.
- Add module-level docstrings to worker submodules (`core.py`, `poll_loops.py`, `periodic_passes.py`), describing each mixin's role in the event-driven consumer assembly.
- Fix SQLite engine leak in Alembic migrations: `alembic/env.py` now disposes its engine after each run, and `init_db` skips redundant `create_all` + Alembic passes when the board is already initialized. Together these eliminate a file-descriptor leak that caused "unable to open database file" errors in CI under test suites with many tests sharing a worker process.
- Fix infinite auto-approval loop: the mechanical draft fast-path now rejects empty/whitespace drafts, preventing tickets with empty descriptions from being auto-approved in a cycle (approve → refine produces empty body → fast-path approves again).
- ci_fix: rebase onto main before scanning CI so stale branches produce a fresh run against current main; include branch HEAD SHA in the consecutive-identical failure fingerprint to prevent the re-block loop on already-resolved upstream failures; clear depends-on after spawning an out-of-scope dependency fix so the operator's resume-blocked is not silently parked by the unmet-dependency gate
- Changed the Pydantic default for `api_host` from `"127.0.0.1"` to `"0.0.0.0"` to match the shipped `config/config.example.json`. Updated `docs/config/configuration.md` accordingly, closing a three-way config-drift gap.
- Fix stale `tester` reference in `docs/agents/agent-yaml-schema.md` — renamed to `run_tests` to match the renamed agent definition.
- Remove 11 backward-compat aliases (`AuditPassResult`, `AgentCheckPassResult`, etc.) from `periodic_runner.py`; all callers now import `PeriodicPassResult` directly.
- Register five missing CLI subcommands (`state-sync`, `env-doc-sync`, `frontend-sync`, `security-posture`, `triage-boilerplate`) in argparse so they are reachable from the command line.
- Deduplicate ``_resolve_repo_config`` by delegating repo-id resolution to
  ``_resolve_repo_id``; collapse three identical ``elif`` arms in
  ``_run_and_print`` into a single ``elif cmd in (...)`` block.
- Change all 14 built-in periodic workflow defaults from daily (86400 s) to weekly (604800 s): `agent_check`, `bc_check`, `completeness_check`, `diagnostic`, `env_doc_sync`, `frontend_sync`, `health`, `meta`, `module_curator`, `repo_description_sync`, `run_health`, `state_sync`, `survey`, `test_gap`. Per-repo overrides via `.robotsix-mill/periodic/<name>.yaml` (`interval:` field) are unchanged — repos that need faster cadence can override back to `1d`.
- Sandbox test timeouts (rc=124) now produce a deterministic ENV-ERROR diagnosis
  instead of invoking the expensive LLM distiller, letting the fix-loop circuit
  breaker fire immediately without burning 30+ requests per cycle.
