Turn recurring CI failures into prevention rules in the implement memory ledger.
  The ci-fix stage now buckets every `CI_FAILURE` event (`ruff-format`, `mypy`,
  `pytest-failure`, …) with a root cause and default prevention rule, and emits a
  paired `CI_FIX_RESOLVED` event when its agent turns CI green. A new daily
  `ci_prevention_rules` periodic pass distils a board's recent failures into at
  most `ci_prevention_max_rules` imperative rules and rewrites a
  `## CI prevention rules (auto-maintained)` section at the top of the implement
  memory ledger in place. The `recurring_ci_failure` diagnostic check no longer
  files `[diagnostic] recurring CI failure` report tickets (it is summary-only;
  `diagnostic_ci_failure_threshold` is kept but inert).
