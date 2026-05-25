# Agent catalog

**Agent definitions live in `agent_definitions/*.yaml`.** The Python
modules under `src/robotsix_mill/agents/` contain the runtime logic
(output models, entry functions, tool implementations) but not the
prompts, tool lists, or model bindings. See
[Agent YAML schema](agent-yaml-schema.md) for the field reference.

---

## Pipeline agents

Run as stages on each ticket in the order: refine → approve → implement → deliver → merge → retrospect.

| Agent | Definition | Module | Model var | Trigger | Role |
|---|---|---|---|---|---|
| Refine | `agent_definitions/refine.yaml` | `agents/refining.py` | `MILL_REFINE_MODEL` | `refine` stage (DRAFT state) | Turns rough draft into precise engineering spec grounded in the repo |
| Implement (coordinator) | `agent_definitions/implement.yaml` | `agents/coordinating.py` | `MILL_MODEL` | `implement` stage (READY state) | Explores, reads, and edits the repo to satisfy the ticket spec |
| Test | (no YAML — `agents/testing.py` constructs directly) | `agents/testing.py` | `MILL_TEST_MODEL` | Called by implement agent as `run_tests` tool | Runs test suite in sandbox; distills failures into actionable diagnosis |
| Deliver | (no agent — `stages/deliver.py` uses forge adapter directly) | — | — | `deliver` stage (DELIVERABLE state) | Pushes branch and opens PR/MR via forge adapter |
| Review | `agent_definitions/review.yaml` | `agents/reviewing.py` | `MILL_REVIEW_MODEL` | `review` stage (CODE_REVIEW state, opt-in via `MILL_REVIEW_ENABLED`) | Blind dual-model audit of git diff against ticket spec |
| Merge (rebase) | (no YAML — `agents/rebasing.py` constructs directly) | `agents/rebasing.py` | `MILL_MODEL` | `merge` stage when PR is conflicting, or when the implement-stage defensive rebase fails | Resolves git merge conflicts on stale branch |
| Merge (CI-fix) | (no YAML — `agents/ci_fixing.py` constructs directly) | `agents/ci_fixing.py` | `MILL_MODEL` | `merge` stage when PR has failing CI (`FIXING_CI` state) | Auto-fixes failing remote CI checks on a PR branch |
| Retrospect | `agent_definitions/retrospect.yaml` | `agents/retrospecting.py` | `MILL_RETROSPECT_MODEL` | `retrospect` stage (DONE state) | Analyses finished ticket workflow + Langfuse traces; proposes pipeline improvements |

## Periodic / on-demand agents

Opt-in agents that run independently of the ticket pipeline.

| Agent | Definition | Module | Model var | Trigger | Role |
|---|---|---|---|---|---|
| Audit | `agent_definitions/audit.yaml` | `agents/auditing.py` | `MILL_AUDIT_MODEL` | CLI (`audit`), API (`POST /audit`), board button, or periodic (`MILL_AUDIT_PERIODIC`) | Meta-audit: identifies gaps in repo quality/security tooling coverage; emits improvement drafts |
| Trace-health | (no agent — deterministic check in `trace_health_runner.py`) | — | — | CLI (`trace-health`), API (`POST /trace-health`), board button, or periodic (`MILL_TRACE_HEALTH_PERIODIC`) | Scans Langfuse for unsessioned traces; files alert draft |
| Health | `agent_definitions/health.yaml` | `agents/health.py` | `MILL_HEALTH_MODEL` | CLI, API (`POST /health-check`), or periodic (`MILL_HEALTH_PERIODIC`) | Codebase-health inspection across 6 dimensions (size, length, docs, tests, complexity, dead code) |
| Test-gap | `agent_definitions/test_gap.yaml` | `agents/test_gap.py` | `MILL_TEST_GAP_MODEL` | CLI, API (`POST /test-gap`), or periodic (`MILL_TEST_GAP_PERIODIC`) | Identifies modules with zero dedicated unit-test coverage |
| Agent-check | `agent_definitions/agent_check.yaml` | `agents/agent_check.py` | `MILL_AGENT_CHECK_MODEL` | CLI, API (`POST /agent-check`), or periodic (`MILL_AGENT_CHECK_PERIODIC`) | Meta-agent: inspects all agent definitions for tool–prompt mismatch, skill drift, metadata correctness, registration completeness, prompt self-consistency, and memory ledger coherence |
| Survey | `agent_definitions/survey.yaml` | `agents/surveying.py` | `MILL_SURVEY_MODEL` | CLI, API (`POST /survey`) | Discovers similar OSS projects via web research; proposes concrete improvements |
| BC-check | `agent_definitions/bc_check.yaml` | `agents/bc_check.py` | `MILL_BC_CHECK_MODEL` | CLI, API (`POST /bc-check`), or periodic (`MILL_BC_CHECK_PERIODIC`) | Backward-compatibility scanner: examines git history for changed signatures and flags breakage |
| Completeness-check | `agent_definitions/completeness_check.yaml` | `agents/completeness_check.py` | `MILL_COMPLETENESS_CHECK_MODEL` | CLI or periodic (`MILL_COMPLETENESS_CHECK_PERIODIC`) | Scans the repo for incomplete feature wiring (missing YAML mappings/defaults, routes without buttons, runners without CLI, agent files without callers) |
| Answer | `agent_definitions/answer.yaml` | `agents/answering.py` | `MILL_ANSWER_MODEL` | `answer` stage (ASKED state — ticket type `inquiry`) | Investigative analyst: answers questions using repo exploration + web research + Langfuse data |

## Sub-agents

Used as tools by primary agents.

| Agent | Definition | Module | Model var | Called by | Role |
|---|---|---|---|---|---|
| Explore | (no YAML — built by `make_explore_tool()`) | `agents/explore.py` | `MILL_EXPLORE_MODEL` | Refine, Implement, Review, Retrospect, Audit, Health, Survey, Answer, Agent-check, Test-gap | Read-only scout: returns concise paths/symbols/line-ranges, never whole files |
| Web-research | (no YAML — `build_agent` called directly) | `agents/web_research.py` | `MILL_WEB_RESEARCH_MODEL` | Refine, Audit, Survey, Answer, Health, Agent-check | Searches the web; returns one concise factual conclusion |
| Dedup | (no YAML — `build_agent` called directly) | `agents/dedup.py` | `MILL_DEDUP_MODEL` | Refine stage (pre-refine guard) | Checks whether draft is duplicate or already implemented; short-circuits to CLOSED |
| Scope-triage | `agent_definitions/scope_triage.yaml` | `agents/scope_triage.py` | `MILL_SCOPE_TRIAGE_MODEL` | Implement stage (scope-violation guard) | Cheap classifier: EXPAND (legitimate out-of-scope change), REJECT (scope creep), or ESCALATE (uncertain) |
| Trace-inspector | (no YAML — `build_agent` called directly) | `agents/trace_inspector.py` | `MILL_TRACE_INSPECTOR_MODEL` | Retrospect | Inspects full Langfuse trace observation tree |
| Cross-trace-analyzer | (no YAML — `make_cross_trace_analyze_tool` closure) | `agents/cross_trace_analyzer.py` | `MILL_TRACE_INSPECTOR_MODEL` | Retrospect | Analyses per-trace summaries for cross-stage patterns (redundant exploration, information loss, retry cascades, context waste, stage inefficiencies) |

## Agent infrastructure

Shared modules used to build and equip agents.

| Module | File | Role |
|---|---|---|
| Agent factory | `agents/base.py` | `build_agent()` / `build_agent_from_definition()` — pydantic-ai Agent factory over OpenRouter; injects tools, `web_research`, `report_issue`, skills |
| YAML loader | `agents/yaml_loader.py` | `load_agent_definition()` — parses and validates `agent_definitions/*.yaml` files |
| File-system tools | `agents/fs_tools.py` | Sandboxed file-system tools (`read_file`, `write_file`, `edit_file`, `delete_file`, `list_dir`, `run_command`) |
| Web tools | `agents/web_tools.py` | `web_fetch` tool (HTTP GET via isolated network container) |
| Report issue | `agents/report_issue.py` | `report_issue` tool (dedup-guarded, injected into every agent) |
| Read ticket | `agents/read_ticket.py` | `read_ticket` tool (read-only counterpart to `report_issue`; injected into periodic agents) |
| Reply to thread | `agents/reply_thread.py` | `reply_to_thread` tool (replies to a comment thread on the current ticket; injected into implement agent) |
| Retry | `agents/retry.py` | Bounded retry with exponential backoff for transient network failures |
| Tool registry | `agents/tool_registry.py` | System-wide catalog of tool capabilities for prompt injection (not an agent registry) |

## See also

- [index.md](index.md) — documentation home
- [agent-yaml-schema.md](agent-yaml-schema.md) — Field reference for `agent_definitions/*.yaml` files
- [expert-yaml-schema.md](expert-yaml-schema.md) — Field reference for `expert_definitions/*.yaml` files
- [docs/configuration.md](configuration.md) — full env-var reference (maps every model var to its agent)
- [docs/audit-agent.md](audit-agent.md) — audit agent deep dive
- [docs/trace-health.md](trace-health.md) — trace-health check deep dive
- [docs/retrospect-memory.md](retrospect-memory.md) — retrospect memory ledger
- [docs/merge-stage.md](merge-stage.md) — merge stage (rebase + CI-fix)
