## 0.0.0 (unreleased)

- Bump anchore/sbom-action from v0.21.0 to v0.24.0 in `docker-publish.yml` (PR #617).
- Dockerfile: drop redundant system `claude` CLI (`npm install -g @anthropic-ai/claude-code`); the `claude-agent-sdk` Python dep ships its own bundled binary (`_bundled/claude`) and prefers it. Also document the `~/.claude` rw mount in `docker-compose.override.example.yml`.
- `_verify_merge_ancestor`: add content-level fallback when `git merge-base --is-ancestor` fails — diffs the feature commit against the target branch and checks whether changed files on the target branch contain the ticket ID. Catches squash and rebase merges where the log message does not mention the ticket.
- Fix two dead references to `runners.security_posture_runner` (consolidated into `runners.periodic_runner`), preventing runtime import failures in the CLI and HTTP pass routes.
- Extend `_triage_outcome` with optional `state` parameter, refactor 8 inline triage sites to use the shared helper
- Fix `board_manager.max_concurrent` documented default in `docs/configuration.md` from `3` to `1`, matching the Pydantic model default.
- Extract `_retry_after_401()` helper in `GitHubForgePRMixin`, centralizing
  the 401 token-invalidation + backoff boilerplate that was duplicated across
  `_create_pr`, `_get_pr`, `_list_branches`, `_list_open_pr_branches`,
  `_list_open_prs`, and `_pr_review_status`.
- Split `_tickets.py` (1169 lines, 25 routes) into three sibling modules:
  `_tickets.py` (core CRUD), `_tickets_merge.py` (merge/CI routes), and
  `_tickets_transitions.py` (state transitions & enrichment). Follows the
  existing route-module pattern and keeps merge-specific imports out of
  the CRUD module.
- Add CLI subcommand `roadmap-sync` with `--json` and `--repo-id` flags, matching the existing POST route at `/roadmap-sync`.
- Wire `triage_boilerplate` agent into all five on-demand dispatch layers (CLI ``_RUNNERS`` entry, POST route, board button, JS handler + window export, ``AGENT_COLORS`` / ``SOURCE_CLASS`` / CSS badge) matching all 13 other ``llm_agent`` peers.
- Split ``forge/gitlab.py`` (monolithic 1131-line ``GitLabForge``) into a ``forge/gitlab/`` package mirroring the GitHub adapter architecture:
  - ``core.py`` — MR lifecycle, branches, repo CRUD
  - ``ci.py`` — ``GitLabForgeCIMixin`` with pipeline status, job log retrieval
  - ``code_scanning.py`` — ``GitLabForgeCodeScanningMixin`` (placeholder)
  - ``dependabot.py`` — ``GitLabForgeDependabotMixin`` (placeholder)
  - ``_pagination.py`` — shared ``_paginated_get`` helper
  - ``__init__.py`` — public re-exports preserving the existing import API
- Align `web_knowledge_request_limit` in `config/config.example.yaml` (was 8) with the model default of 12.
- Add test coverage for refine stage submodules: `_reconcile.py`, `_result_paths.py`, and `_triage.py` (146 tests across three new test files)
- Add unit test coverage for ``MultiRepoCiFixMixin`` (``tests/stages/merge/test_ci_fix_mixin.py``) covering all three private methods with 29 test cases.
- Add `.robotsix-mill/periodic/security_posture.yaml` trigger file to enable the security_posture periodic workflow in mill.
- Broadened mandatory `read_file` verification in trace inspector from optimization-only to ALL finding categories when `root_cause` or `proposed_solution` makes a mechanistic claim about code behaviour.
- web_knowledge: add `last_verified` frontmatter field (bumped on every `update_library`), `stale` boolean field, and cache TTL (`web_knowledge_cache_ttl_hours`, default 72 h). Entries older than the TTL are flagged `[STALE]` in the index so the web-knowledge agent re-verifies before trusting cached claims.
- Add stale re-spawn guard to implement stage preflight: when the last
  implement attempt was blocked and the spec hasn't changed, fail fast
  before opening a Langfuse trace — preventing $0.00 no-op traces and
  redundant paid re-spawns.  The convergence backstop (empty diff after
  review) now also writes ``implement.md`` so the guard can prevent the
  same no-op on the next attempt.
- Fix `config.example.yaml` and `web_knowledge.py` tool-description strings to reflect the new `web_knowledge_request_limit` default of 12 (follow-up to the bump from 8→12).
- Increase `web_knowledge_request_limit` default from 8 to 12 to prevent
  budget-exhaustion errors during multi-step web-knowledge consultations.
- Enable the `security_posture` periodic agent for mill by adding the per-repo
  opt-in trigger at `.robotsix-mill/periodic/security_posture.yaml`.
- Added `source_url` and `verified_at` tracking to the web-knowledge agent's
  library cache. The `update_library` tool now accepts an optional
  `source_url` parameter; when provided, a `verified_at` timestamp is
  recorded so `read_library` can distinguish file-touch from
  fact-verification. Updated `_parse_frontmatter` to return a
  `_KnowledgeMeta` dataclass with all frontmatter fields. Updated the
  system prompt to instruct the agent to treat unverified claims as suspect
  regardless of `last_updated` recency — preventing stale cached facts from
  cascading into 404s.
- Add `Stage.preflight()` — a lightweight pre-trace gate that lets stages signal early-exit before a Langfuse trace is opened. The implement stage uses it to catch empty specs and exceeded cycle limits without consuming a spawn slot or emitting a $0.00 trace.
- Remove five dead backward-compat re-exports from `orchestration.py`: `_persist_triage_complexity`, `_MIGRATE_NOTE_PREFIX`, `_anti_bounce_escalate`, `_parse_prior_boards`, `_is_sendback_reentry` (zero imports across the codebase).
- Add implement-stage precondition checks: spec emptiness gate and per-ticket spawn counter (`implement_max_spawns_per_ticket`, default 3) to fail fast on known no-op conditions instead of burning paid LLM invocations on empty-spec or runaway re-spawn loops.
- Remove stale `.robotsix-board-agent-src` git submodule; the board-agent package is already managed by uv in `pyproject.toml` and the submodule reference pointed to a commit no longer reachable on the remote, breaking Dependabot's configured graph update
- **cleanup**: remove unused ``SecurityPosturePassResult`` backward-compat
  alias from ``periodic_runner.py`` and its vulture whitelist entry.
- ``trace_observation_summary()`` now falls back to trace-level
  ``metadata.mill.step_usage`` when the Langfuse list endpoint returns
  zero observations, so cost-attribution works even without fetching
  per-trace detail (the list endpoint does not include the observation
  tree).
- Add ``backend`` billing tag (``"claude_sdk"`` vs ``"openrouter"``) to
  per-step usage data emitted by ``record_step_usage()``, and surface it
  in ``trace_observation_summary()`` so the cost-analyst can distinguish
  subscription (estimate-only) cost from real marginal cost.
- Fix `trace_observation_summary()` to read per-step usage data from
  `langfuse.observation.metadata.mill.step_usage` span attributes when
  GENERATION observations are absent. The `record_step_usage()` write
  path now uses the `langfuse.observation.metadata.` prefix so the
  attribute is visible through Langfuse's REST API (previously it was
  stored as a raw span attribute inaccessible to the read path).
- **tests**: reorganize runtime test files into per-subpackage subdirectories
  (``tests/runtime/routes/``, ``tests/runtime/worker/``) mirroring the source
  layout.  Harness ``.mjs`` paths updated accordingly.
- **agents**: remove dead backward-compat alias ``TriageBoilerplateResult``
  (no callers in ``src/`` or ``tests/``); update YAML definition to reference
  ``PeriodicAgentResult`` directly. (mill: Remove dead backward-compat alias `TriageBoilerplateResult` from triage_boilerplate.py (20260630T211442Z-remove-dead-backward-compat-alias-triage-3eb1))
- **dev**: add ``make format`` and ``make lint`` targets for quick local
  ruff/mypy checks across all Python sources (``src/``, ``tests/``,
  ``scripts/``, ``vulture_whitelist.py``, ``deploy/split_config.py``,
  ``dev/``).  ``lint`` is check-only; ``format`` auto-fixes.
- **deps**: update robotsix-board-agent pin to commit adding
  ``handle_wrapper`` param to ``BoardManager.__init__`` (Step 0 of
  board-manager fast-lane epic).  Push of the upstream commit requires
  human GitHub credentials; ``uv lock`` must be re-run afterwards.
- **stages**: reorganize test layout to mirror source subdirectory structure.
  Move refine tests to ``tests/stages/refine/`` and merge tests to
  ``tests/stages/merge/``, matching the existing ``tests/stages/implement/``
  pattern.  Add ``__init__.py`` files to new test subdirectories and update
  ``docs/modules.yaml`` paths accordingly.

- **git**: fix ``git_push_with_lease`` for first-push branches (no remote
  counterpart yet).  The pre-push fetch now tolerates "couldn't find remote
  ref" and falls through to ``push_with_lease`` which already handles
  first-push via ``--force``.  Genuine network/auth fetch errors still
  surface as ``PUSH_ERROR``.
- **scripts**: fix misaligned trace-ID↔latency pairs in
  ``_extract_board_traces()`` per-trace table by sorting
  ``board_traces`` and ``latencies_s`` in lockstep instead of sorting
