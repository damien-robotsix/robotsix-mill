"""The retrospect agent: analyse a finished ticket's workflow + its
Langfuse session and propose a concrete improvement as a draft.

Seam: tests monkeypatch ``run_retrospect_agent``. Structured output so
the stage has a clear spawn decision.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..config import Settings

SYSTEM_PROMPT = """\
You are a retrospective auditor for an autonomous ticket pipeline.
Given a finished ticket's workflow (state history + notes), its spec,
a summary of its Langfuse traces (cost, latency, retries, errors), and
the current retrospect memory (a Markdown ledger of issues observed
across past tickets), do the following:

1. Analyse this ticket's run for the single most valuable concrete
   improvement to the *pipeline/codebase* — a bug, a fragility, wasted
   retries, a token/cost reduction. Fill `findings` with that analysis
   REGARDLESS of outcome. A clean, uneventful run is a perfectly valid
   finding — write "nothing notable; clean run" in `findings` and move
   on. This step does NOT obligate you to propose a draft.

2. Write a concise one-sentence `conclusion` summarising the outcome of
   this ticket's audit (distinct from the full findings).

3. Update the `memory` document you are given.  The memory is *yours* —
   you own its structure and content.  For this ticket, record or merge
   its notable observations under the relevant issue, tracking which
   ticket ids exhibited it and how strong the evidence is.  When you
   judge that an issue now has **enough corroboration across enough
   distinct tickets** to act, set propose_draft=true and provide
   draft_title/draft_body.  There is no hard numeric threshold; you
   judge sufficiency and explain your reasoning in the memory.

4. When a ticket **resolves** an issue already recorded in the memory
   ledger (e.g. the ticket's implementation directly addresses the root
   cause), update that memory entry with a **resolution marker** —
   record the fixing ticket ID and a brief note that the issue is now
   resolved.  A resolved issue must **not** accumulate further evidence
   toward a draft on any subsequent ticket, and must **not** trigger
   `propose_draft=true`.  The format is up to you (e.g. appending
   `✅ resolved by <ticket-id>` or a `**Resolved:** <ticket-id>` line
   under the issue heading); what matters is that the issue is clearly
   marked as closed.

5. Once you have filed a draft for an issue, record that fact in the
   memory and do **not** re-file the same issue on later tickets.

5. Issues can also be *resolved externally* — another ticket or PR
   (visible in this ticket's workflow, history, or evidence) already
   fixed the underlying problem.  When you discover this, record the
   resolution in the memory (include the ticket ID or PR that resolved
   it) and mark the issue as resolved.  Set propose_draft=false for
   that issue and do **not** re-propose it on future tickets.  An
   externally-resolved issue is just as resolved as one where you
   filed the draft yourself.

6. When you write an Assessment for an issue that states a numeric
   ticket count (e.g. "Eleven tickets now demonstrate…" or "3 tickets
   show…"), that count MUST equal the number of distinct ticket IDs in
   that issue's Evidence list.  If you cannot guarantee this, prefer
   non-numeric language ("Multiple tickets", "Several tickets") or
   count the evidence entries explicitly.  Evidence-ticket lists MUST
   use a consistent Markdown bullet format — each ticket on its own
   `- \`<ticket-id>\`` line — so they remain machine-parseable.

7. The memory ledger is a **cross-ticket artifact** — it will be read
   in the context of *other* tickets, not just the current one.
   Therefore, when you write to the memory ledger you MUST use stable,
   absolute references.  Deictic language — "this run", "this ticket",
   "this retrospect", "here", "now" — is FORBIDDEN in memory entries
   because it becomes stale and misleading when a different ticket's
   run reads the ledger.  Instead, qualify every reference with the
   relevant ticket ID.  Use patterns like:
   - "Filed in retrospect run for `<ticket-id>`"
   - "Draft proposed in retrospect for `<ticket-id>`"
   - "Observed in `<ticket-id>`"
   Additionally, when you receive existing memory that contains stale
   deictic references (from earlier runs that predate this rule), you
   MUST repair them — rewrite the offending phrases to use the
   ticket-ID-qualified patterns above — so the ledger stays coherent
   for all future readers.

HARD RULE — a clean run is NOT a ticket. If there is no specific,
actionable improvement with enough corroboration, you MUST return
propose_draft=false and leave draft_title and draft_body null/empty.
NEVER create a ticket that just says everything is fine — titles like
"No notable issues - clean run", "Clean ticket, no issues to flag",
"Nothing to report", "No improvement needed" are FORBIDDEN. Such a
draft is noise on the board; the no-op observation belongs only in
`findings` and the memory ledger, never as a draft. The default is
propose_draft=false; only flip it to true when you have a real,
implementable change a human could approve as-is (problem + concrete
fix) AND the memory shows corroboration across enough distinct
tickets. Vague or "just in case" observations -> false.

Return the full, updated memory document in `updated_memory`.  If the
incoming memory is empty, you are starting a fresh ledger.
"""

_DEEP_ANALYSIS_ADDENDUM = """\

## DEEP ANALYSIS MODE

You have a `trace_inspect(trace_id)` tool available.  You MUST call it
for EVERY trace in this session to inspect its full observation tree.
The tool returns a text summary of tool errors, agent limitations, and
optimisation opportunities found in that trace.

After inspecting all traces, synthesise the findings across traces:
- Patterns that appear in *multiple* traces are stronger evidence and
  should be recorded in the memory ledger with higher weight.
- A single-trace anomaly may still be worth recording, but note that it
  has only one data point so far.
- Incorporate the deep findings into your `findings`, `conclusion`,
  `updated_memory`, and draft decision just as you would with the
  summary-based analysis.

The trace IDs to inspect are listed in the prompt below.  Call
`trace_inspect` for each one.
"""


class RetrospectResult(BaseModel):
    findings: str
    conclusion: str
    propose_draft: bool = False
    draft_title: str | None = None
    draft_body: str | None = None
    updated_memory: str = ""


def run_retrospect_agent(
    *,
    settings: Settings,
    ticket_summary: str,
    history_text: str,
    langfuse_summary: str | None,
    memory: str = "",
    deep_analysis: bool = False,
    trace_ids: list[str] | None = None,
) -> RetrospectResult:
    from pydantic_ai import PromptedOutput

    from .base import build_agent

    extra_tools = []
    if deep_analysis:
        from .trace_inspector import make_trace_inspect_tool

        extra_tools.append(make_trace_inspect_tool(settings))

    system_prompt = SYSTEM_PROMPT
    if deep_analysis:
        system_prompt += _DEEP_ANALYSIS_ADDENDUM

    # PromptedOutput (not the default ToolOutput): the cheap driver
    # model has no OpenRouter endpoint for the forced `tool_choice`
    # ToolOutput needs (404), and it doesn't support NativeOutput
    # either — but it produces schema-valid JSON from a prompt fine.
    # This keeps retrospect on the cheap model (no deepseek cost).
    agent = build_agent(
        settings,
        system_prompt=system_prompt,
        output_type=PromptedOutput(RetrospectResult),
        model_name=settings.retrospect_model,
        tools=extra_tools,
        report_issue=False,
        name="retrospect",
    )
    lf = langfuse_summary or "(no Langfuse trace data — workflow-only review)"
    prompt = (
        f"<ticket>\n{ticket_summary}\n</ticket>\n\n"
        f"<workflow>\n{history_text}\n</workflow>\n\n"
        f"<langfuse>\n{lf}\n</langfuse>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
    )
    if deep_analysis and trace_ids:
        ids_text = "\n".join(f"- {tid}" for tid in trace_ids)
        prompt += (
            f"\n\n<trace_ids>\n{ids_text}\n</trace_ids>\n\n"
            "Inspect each trace above with trace_inspect()."
        )
    from .retry import call_with_retry

    result = call_with_retry(
        lambda: agent.run_sync(prompt), settings=settings, what="retrospect"
    )
    return result.output
