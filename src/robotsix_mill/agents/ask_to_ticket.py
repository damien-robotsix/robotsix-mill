"""The ask-to-ticket agent: drafts a single actionable task ticket from
an answered inquiry's question + answer + an operator comment, returning
a structured ``AskToTicketResult`` (title + Markdown body).

``run_ask_to_ticket_agent`` is the mockable seam — tests monkeypatch it.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..config import Settings


class AskToTicketResult(BaseModel):
    """The drafted task ticket: an imperative title and a self-contained
    Markdown body."""

    title: str
    description: str


def run_ask_to_ticket_agent(
    *,
    settings: Settings,
    question: str,
    answer: str,
    comment: str,
    repo_dir: Path | None = None,
) -> AskToTicketResult:
    """Draft a task ticket from a Q&A exchange and an operator comment.

    When *repo_dir* is given the agent grounds its draft in that local
    clone via explore/read_file/list_dir/run_command; when None, no repo
    tools are attached. Raises RuntimeError if no OpenRouter key is
    configured.
    """
    from pydantic_ai import PromptedOutput

    from .yaml_loader import load_and_run_agent

    # Repo tools (explore, read_file, list_dir, run_command) are only
    # attached when grounded in a local clone.  The web route passes
    # ``repo_dir=None`` because the draft is composed from the Q&A text
    # and operator comment alone — there is no codebase to ground in.
    from ._repo_tools import _build_repo_tools

    tools = _build_repo_tools(repo_dir, settings)

    # Format the Q&A + comment as XML-delimited blocks so the model can
    # unambiguously separate the three inputs without relying on ad-hoc
    # separators that might appear in the text itself.
    user_prompt = (
        f"<question>\n{question}\n</question>\n"
        f"<answer>\n{answer}\n</answer>\n"
        f"<comment>\n{comment}\n</comment>\n\n"
        "Draft a single, concise, actionable engineering task from the "
        "inquiry above and the operator's comment. Produce a crisp "
        "imperative title and a self-contained Markdown body."
    )

    result = load_and_run_agent(
        settings=settings,
        definition_name="ask_to_ticket",
        tools=tools,
        model_name=settings.ask_to_ticket_model,
        prompt=user_prompt,
        what="ask_to_ticket",
        # Wrap in PromptedOutput (free-text JSON) rather than passing the
        # raw class: a raw BaseModel makes pydantic-ai use ToolOutput, which
        # forces tool_choice — and DeepSeek-v4-pro's reasoning ("xhigh")
        # mode rejects forced tool_choice with a 400. The YAML-driven path
        # in base.py wraps for the same reason; this runner-supplied
        # override must wrap too.
        output_type=PromptedOutput(AskToTicketResult),
    )
    return result.output
