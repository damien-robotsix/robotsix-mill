"""The strong "deep" authoring sub-agent.

Both refine and implement now run a CHEAP driver model (``settings.model``)
that explores, assembles a complete context, applies, and verifies — but
delegates the actual authoring (the precise spec / the precise code
change) to this strong model (``settings.deep_model``), exposed to the
driver as a single tool.

The deep agent has NO tools and runs ~one-shot per call: it receives a
complete, self-contained context string and returns the artifact. This
is what makes the expensive model cost bounded — it is invoked
deliberately by the driver instead of running the whole agentic loop.

``run_deep_refine`` / ``run_deep_implement`` are the mockable seams —
tests monkeypatch them (no key / network), exactly like the other agent
seams.
"""

from __future__ import annotations

from ..config import Settings

_REFINE_PROMPT = """\
You are a senior engineer. You are given a complete, self-contained
context (a ticket title, its rough draft, and any research the caller
gathered). Produce a precise, self-contained engineering spec for an
autonomous coding agent that will implement it WITHOUT asking
questions.

Output Markdown only, with exactly these sections:
- ## Problem — what and why, one short paragraph.
- ## Scope — bullet list of concrete changes to make.
- ## Acceptance criteria — checklist an automated reviewer can verify.
- ## Out of scope / constraints — what NOT to do, assumptions.

Stay faithful to the draft's intent; invent nothing unrelated. Be
concrete and testable. Output the spec only — no preamble, no fences.
"""

_IMPLEMENT_PROMPT = """\
You are a senior engineer. You are given a complete, self-contained
context: the spec, the FULL current content of every relevant file,
project conventions, and (on a retry) the failing test output.

Return the precise change as a patch/plan the caller will apply
mechanically — do NOT assume you can run anything:
- A one-paragraph plan.
- For each file to create or modify: a fenced block headed
  `FILE: <repo-relative-path>` containing that file's COMPLETE final
  content (not a diff fragment — the whole file as it should end up).
- A final line `DELETE: <path>, <path>` for any files to remove
  (omit the line if none).

Make the smallest change that fully satisfies the spec, match the
surrounding style, and add/adjust tests for the behaviour you change.
Output only the plan + file blocks — no other commentary.
"""


def _run_deep(*, settings: Settings, system_prompt: str, context: str) -> str:
    if not settings.openrouter_api_key:
        return "deep model unavailable: OPENROUTER_API_KEY is not set"

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .openrouter_cost import CostInstrumentedOpenRouterModel

    model = CostInstrumentedOpenRouterModel(
        settings.deep_model,  # never ":online" — no web on the deep agent
        provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
    )
    agent = Agent(model=model, system_prompt=system_prompt, output_type=str)
    limits = UsageLimits(request_limit=settings.deep_model_request_limit)
    try:
        result = agent.run_sync(context, usage_limits=limits)
    except Exception as e:  # noqa: BLE001 — degrade, don't break the driver
        return f"deep model failed: {e}"
    return str(result.output).strip()


def run_deep_refine(*, settings: Settings, context: str) -> str:
    """Strong-model spec authoring from a complete context."""
    return _run_deep(
        settings=settings, system_prompt=_REFINE_PROMPT, context=context
    )


def run_deep_implement(*, settings: Settings, context: str) -> str:
    """Strong-model code-change authoring from a complete context.
    Returns a plan + full final file contents the driver applies."""
    return _run_deep(
        settings=settings, system_prompt=_IMPLEMENT_PROMPT, context=context
    )


def make_deep_refine_tool(settings: Settings):
    def deep_refine(context: str) -> str:
        """Hand the COMPLETE refinement context (ticket title, full
        draft, any research findings) to the strong authoring model and
        get back the finished Markdown spec. Pass everything needed —
        this model has no tools and cannot look anything up."""
        return run_deep_refine(settings=settings, context=context)

    return deep_refine


def make_deep_implement_tool(settings: Settings):
    def deep_implement(context: str) -> str:
        """Hand the COMPLETE implementation context (spec, full current
        content of every file you may touch, conventions, and on a
        retry the failing test output) to the strong authoring model.
        Returns a plan plus the full final content of each changed file
        for you to apply with write_file. This model has no tools and
        cannot read the repo — include everything it needs."""
        return run_deep_implement(settings=settings, context=context)

    return deep_implement
