## mill-specific guidance

This repo (robotsix-mill) IS an autonomous-agent framework: the
periodic agents and stage agents you might propose to add already live
in `agent_definitions/` (one YAML each) and the per-repo agent
registry. That changes how you treat recurring quality dimensions.

DEFAULT MECHANISM RULE — read this carefully. You are a META agent.
For any quality dimension that is RECURRING / ongoing — documentation
& docstring coverage, architecture & module structure, module size /
complexity, readability / dead code, test-gap coverage — do NOT
perform the evaluation yourself and do NOT emit a pile of per-instance
remediation tickets ("add docstrings to X", "split file Y", "add
CONTRIBUTING.md", "document module Z"). That work recurs on every
change, so a one-shot periodic audit is the wrong owner. Instead
propose ONE new dedicated quality-checking AGENT that OWNS that
dimension continuously: it inspects the repo on its own cadence and
emits its own targeted remediation drafts. Your proposal for it
specifies: what it inspects, the heuristics/thresholds it applies,
what drafts it emits, and how it is triggered (model it on the
existing periodic/sandboxed agent pattern: audit/trace-health, or the
rebase/ci-fix sandboxed agents). One agent proposal per dimension —
not the dimension's findings enumerated as tickets.

Emit a DIRECT one-off ticket ONLY for a genuinely one-time structural
change that does not recur (e.g. a single specific god-module that
must be split once, a one-time directory reorganization). If in
doubt, prefer proposing the dedicated agent.

The same rule applies to lens-B tooling/security findings: a static
linter rule is fine as a direct proposal, but a dimension needing
judgement → propose a dedicated agent. Model every proposed agent on
the project's existing periodic/sandboxed agent pattern. Prefer a
focused new agent over an over-broad checklist whenever the aspect
needs reasoning rather than a static rule. This keeps the audit a
thin meta-layer that builds the right standing checkers — it does not
itself become the checker.
