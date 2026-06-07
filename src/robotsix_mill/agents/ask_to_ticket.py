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
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import run_agent

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "ask_to_ticket.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t
            for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        model_name=definition.model or settings.ask_to_ticket_model,
        output_type=AskToTicketResult,
    )

    user_prompt = (
        f"<question>\n{question}\n</question>\n"
        f"<answer>\n{answer}\n</answer>\n"
        f"<comment>\n{comment}\n</comment>\n\n"
        "Draft a single, concise, actionable engineering task from the "
        "inquiry above and the operator's comment. Produce a crisp "
        "imperative title and a self-contained Markdown body."
    )

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(user_prompt),
            settings=settings,
            what="ask_to_ticket",
        )
    finally:
        _safe_close(agent)
    return result.output
