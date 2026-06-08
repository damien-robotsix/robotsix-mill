"""The answering agent: an investigative analyst that answers questions
using all available tools — repo exploration, web research, and Langfuse
trace data — and returns a free-form Markdown answer.

``run_answer_agent`` is the mockable seam — tests monkeypatch it.
"""

from __future__ import annotations

from pathlib import Path

from ..config import RepoConfig, Settings
from .langfuse_tools import _build_langfuse_tools  # noqa: F401 — re-export


def run_answer_agent(
    *,
    settings: Settings,
    title: str,
    question: str,
    repo_dir: Path | None = None,
    repo_config: RepoConfig | None = None,
) -> str:
    """Return a free-form Markdown answer string. When *repo_dir* is
    given the agent grounds its answer in that local clone via
    explore/read_file/list_dir/run_command. Always has web_research
    and Langfuse tools. Raises RuntimeError if no OpenRouter key is
    configured.

    When *repo_config* is provided, its Langfuse credentials are
    forwarded to the Langfuse tools so the agent queries the repo's
    own Langfuse project instead of the global one.
    """
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import run_agent

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "answer.yaml"
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

    # Langfuse read tools — always available
    langfuse_tools = _build_langfuse_tools(settings)
    tools.extend(langfuse_tools)

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        model_name=definition.model or settings.answer_model,
    )

    user_prompt = f"<title>{title}</title>\n<question>\n{question}\n</question>\n\nAnswer the question above. Cite all sources."

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(user_prompt),
            settings=settings,
            what="answer",
        )
    finally:
        _safe_close(agent)
    return str(result.output).strip()
