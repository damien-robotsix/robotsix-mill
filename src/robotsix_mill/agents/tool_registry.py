"""A system-wide, programmatically-queryable catalog of tool capabilities.

pydantic-ai auto-generates per-agent tool schemas at inference time
from each callable's signature + docstring. That tells each agent what
*it* can call.  This registry fills the gap: it tells planning/refine
agents what *could* exist system-wide so they don't hallucinate tools
that don't exist or miss ones they should plan for.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ToolCategory = Literal["fs", "shell", "exploration", "testing", "web", "reporting"]

_CATEGORY_ORDER: dict[ToolCategory, int] = {
    "fs": 0,
    "shell": 1,
    "exploration": 2,
    "testing": 3,
    "web": 4,
    "reporting": 5,
}


class ToolInfo(BaseModel):
    """Describes one tool registered in the system."""

    name: str  # e.g. "read_file"
    description: str  # one-line summary from docstring
    category: ToolCategory
    parameters: dict[str, str] = {}  # param_name → type (e.g. {"path": "str"})


class ToolRegistry:
    """Class-level singleton catalog of all tool capabilities.

    Tools are registered explicitly at their construction site (not via
    decorators or AST inspection) so the codebase stays grep-friendly.
    """

    _tools: dict[str, ToolInfo] = {}

    @classmethod
    def register(cls, tool: ToolInfo) -> None:
        """Insert *tool* by name.  Overwrites on duplicate name (last
        registration wins)."""
        cls._tools[tool.name] = tool

    @classmethod
    def list_tools(cls) -> list[ToolInfo]:
        """Return every registered tool, sorted by category then name."""
        return sorted(
            cls._tools.values(),
            key=lambda t: (_CATEGORY_ORDER.get(t.category, 99), t.name),
        )

    @classmethod
    def describe_for_prompt(cls) -> str:
        """Return a concise Markdown table of all registered tools,
        grouped by category, suitable for injection into a system prompt.

        When the registry is empty, returns a short message instead of
        an empty table that would confuse the model.
        """
        tools = cls.list_tools()
        if not tools:
            return (
                "## Available tools\n\n"
                "(No tools have been registered yet — "
                "the tool registry is empty.)\n"
            )

        lines: list[str] = ["## Available tools", ""]
        current_cat: str | None = None

        for t in tools:
            if t.category != current_cat:
                current_cat = t.category
                lines.append(f"### {current_cat}")
                lines.append("")
                lines.append("| Tool | Category | Description |")
                lines.append("|------|----------|-------------|")
            lines.append(f"| {t.name} | {t.category} | {t.description} |")

        lines.append("")
        lines.append(
            "> Prefer direct tools (read_file, list_dir, run_command) "
            "for single-step lookups.\n"
            "> Use explore only for complex multi-step questions; "
            "batch related questions into ONE explore call."
        )

        return "\n".join(lines)
