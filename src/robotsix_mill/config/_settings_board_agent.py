"""Settings field mixin: board-agent (agent-comm bridge to the mill board API).

Field-only pydantic mixin. Assembled into the final ``Settings`` class
in ``config/settings.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _BoardAgentSettings(BaseModel):
    """Opt-in agent-comm board-agent service (off by default).

    When ``board_agent_enabled`` is True the mill starts a
    ``robotsix_board_agent.BoardAgent`` instance that bridges
    agent-comm messages to the mill board REST API, allowing other
    agents to drive the board programmatically.
    """

    board_agent_enabled: bool = Field(default=False)
    board_agent_api_url: str = Field(default="http://localhost:8000")
    board_agent_api_token: str = Field(default="")
    board_agent_repo_id: str = Field(default="")
    board_agent_write_ops: bool = Field(default=True)
