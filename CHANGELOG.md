## 0.0.0 (unreleased)

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
