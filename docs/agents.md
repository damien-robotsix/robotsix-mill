# Agent catalog

Every agent in `src/robotsix_mill/agents/`, grouped by category.

---

## Pipeline agents

Run as stages on each ticket in the order: refine → approve → implement → deliver → merge → retrospect.

| Agent | File | Model var | Trigger | Role |
|---|---|---|---|---|
| Refine | `agents/refining.py` | `MILL_REFINE_MODEL` | `refine` stage (DRAFT state) | Turns rough draft into precise engineering spec grounded in the repo |
| Implement (coordinator) | `agents/coordinating.py` | `MILL_MODEL` | `implement` stage (READY state) | Explores, reads, and edits the repo to satisfy the ticket spec |
| Test | `agents/testing.py` | `MILL_TEST_MODEL` | Called by implement agent as `run_tests` tool | Runs test suite in sandbox; distills failures into actionable diagnosis |
| Deliver | (no agent — `stages/deliver.py` uses forge adapter directly) | — | `deliver` stage (DELIVERABLE state) | Pushes branch and opens PR/MR via forge adapter |
| Review | `agents/reviewing.py` | `MILL_REVIEW_MODEL` | `review` stage (CODE_REVIEW state, opt-in via `MILL_REVIEW_ENABLED`) | Blind dual-model audit of git diff against ticket spec |
| Merge (rebase) | `agents/rebasing.py` | `MILL_MODEL` | `merge` stage when PR is conflicting | Resolves git merge conflicts on stale PR branch |
| Merge (CI-fix) | `agents/ci_fixing.py` | `MILL_MODEL` | `merge` stage when PR has failing CI (`FIXING_CI` state) | Auto-fixes failing remote CI checks on a PR branch |
| Retrospect | `agents/retrospecting.py` | `MILL_RETROSPECT_MODEL` | `retrospect` stage (DONE state) | Analyses finished ticket workflow + Langfuse traces; proposes pipeline improvements |

## Periodic / on-demand agents

Opt-in agents that run independently of the ticket pipeline.

| Agent | File | Model var | Trigger | Role |
|---|---|---|---|---|
| Audit | `agents/auditing.py` | `MILL_AUDIT_MODEL` | CLI (`audit`), API (`POST /audit`), board button, or periodic (`MILL_AUDIT_PERIODIC`) | Meta-audit: identifies gaps in repo quality/security tooling coverage; emits improvement drafts |
| Trace-health | (no agent — deterministic check in `trace_health_runner.py`) | — | CLI (`trace-health`), API (`POST /trace-health`), board button, or periodic (`MILL_TRACE_HEALTH_PERIODIC`) | Scans Langfuse for unsessioned traces; files alert draft |
| Health | `agents/health.py` | `MILL_HEALTH_MODEL` | CLI, API (`POST /health-check`), or periodic (`MILL_HEALTH_PERIODIC`) | Codebase-health inspection across 6 dimensions (size, length, docs, tests, complexity, dead code) |
| Test-gap | `agents/test_gap.py` | `MILL_TEST_GAP_MODEL` | CLI, API (`POST /test-gap`), or periodic (`MILL_TEST_GAP_PERIODIC`) | Identifies modules with zero dedicated unit-test coverage |
| Agent-check | `agents/agent_check.py` | `MILL_AGENT_CHECK_MODEL` | CLI, API (`POST /agent-check`), or periodic (`MILL_AGENT_CHECK_PERIODIC`) | Meta-agent: inspects all agent definitions for tool–prompt mismatch, skill drift, metadata correctness |
| Survey | `agents/surveying.py` | `MILL_SURVEY_MODEL` | CLI, API (`POST /survey`) | Discovers similar OSS projects via web research; proposes concrete improvements |
| Answer | `agents/answering.py` | `MILL_ANSWER_MODEL` | `answer` stage (ASKED state — ticket type `inquiry`) | Investigative analyst: answers questions using repo exploration + web research + Langfuse data |

## Sub-agents

Used as tools by primary agents.

| Agent | File | Model var | Called by | Role |
|---|---|---|---|---|
| Explore | `agents/explore.py` | `MILL_EXPLORE_MODEL` | Refine, Implement, Review, Retrospect, Audit, Health, Survey, Answer, Agent-check, Test-gap | Read-only scout: returns concise paths/symbols/line-ranges, never whole files |
| Web-research | `agents/web_research.py` | `MILL_WEB_RESEARCH_MODEL` | Refine, Audit, Survey, Answer, Health, Agent-check | Searches the web; returns one concise factual conclusion |
| Dedup | `agents/dedup.py` | `MILL_DEDUP_MODEL` | Refine stage (pre-refine guard) | Checks whether draft is duplicate or already implemented; short-circuits to CLOSED |
| Trace-inspector | `agents/trace_inspector.py` | `MILL_TRACE_INSPECTOR_MODEL` | Retrospect | Inspects full Langfuse trace observation tree |

## Agent infrastructure

Shared modules used to build and equip agents.

| Module | File | Role |
|---|---|---|
| Agent factory | `agents/base.py` | `build_agent()` — pydantic-ai Agent factory over OpenRouter; injects tools, `web_research`, `report_issue`, skills |
| File-system tools | `agents/fs_tools.py` | Sandboxed file-system tools (`read_file`, `write_file`, `edit_file`, `delete_file`, `list_dir`, `run_command`) |
| Web tools | `agents/web_tools.py` | `web_fetch` tool (HTTP GET via isolated network container) |
| Report issue | `agents/report_issue.py` | `report_issue` tool (dedup-guarded, injected into every agent) |
| Retry | `agents/retry.py` | Bounded retry with exponential backoff for transient network failures |
| Tool registry | `agents/tool_registry.py` | System-wide catalog of tool capabilities for prompt injection (not an agent registry) |

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference (maps every model var to its agent)
- [docs/audit-agent.md](audit-agent.md) — audit agent deep dive
- [docs/trace-health.md](trace-health.md) — trace-health check deep dive
- [docs/retrospect-memory.md](retrospect-memory.md) — retrospect memory ledger
- [docs/merge-stage.md](merge-stage.md) — merge stage (rebase + CI-fix)
