"""Settings field mixin: board-agent integration.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _BoardAgentSettings(BaseModel):
    # Board agent — opt-in agent-comm service (off by default)
    board_agent_enabled: bool = Field(default=False)
    board_agent_api_url: str = Field(default="http://localhost:8000")
    board_agent_api_token: str = Field(default="")
    board_agent_repo_id: str = Field(default="")
    board_agent_write_ops: bool = Field(default=True)
    # Central agent-comm broker the board agent registers with (pull/mailbox
    # mode — NAT-safe, outbound-only). When broker_host is set the board agent
    # runs as a BrokeredBoardResponder reachable from off-host clients (e.g. the
    # cost-analyst); broker_token authenticates this agent to the broker.
    board_agent_broker_host: str = Field(default="")
    board_agent_broker_port: int = Field(default=443)
    board_agent_broker_scheme: str = Field(default="https")
    board_agent_broker_token: str = Field(default="")
