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
    from .yaml_loader import load_and_run_agent

    from ._repo_tools import _build_repo_tools

    tools = _build_repo_tools(repo_dir, settings)

    # Langfuse read tools — always available
    langfuse_tools = _build_langfuse_tools(settings, repo_config=repo_config)
    tools.extend(langfuse_tools)

    user_prompt = f"<title>{title}</title>\n<question>\n{question}\n</question>\n\nAnswer the question above. Cite all sources."

    result = load_and_run_agent(
        settings=settings,
        definition_name="answer",
        tools=tools,
        prompt=user_prompt,
        what="answer",
    )
    return str(result.output).strip()
