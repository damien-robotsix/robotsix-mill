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
    def describe_for_prompt(cls, tool_names: set[str] | None = None) -> str:
        """Deprecated. Returns an empty string.

        Tool descriptions are no longer injected into the system
        prompt — pydantic-ai already forwards each tool's signature
        and docstring to the model as a structured ``tools`` array
        on every API call, so a prose Markdown copy was pure token
        duplication. ``compose_prompt`` no longer calls this method;
        the shim stays for any out-of-tree caller still wired to it.
        """
        del tool_names
        return ""
