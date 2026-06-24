"""Settings field mixin: component-agent integration.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class _ComponentAgentSettings(BaseModel):
    # Component agent — generic monitor/config responder on the agent-comm
    # broker. Off by default; enabled when component_agent_enabled=True with a
    # non-empty broker host and bearer token.
    component_agent_enabled: bool = Field(default=False)
    # Agent id registered on the broker. Defaults to "component-robotsix-mill"
    # (NOT "board-manager-robotsix-mill", which is already claimed by the
    # existing BoardManager conversational agent — see docs/component-agent.md).
    component_agent_agent_id: str = Field(default="component-robotsix-mill")
    component_agent_broker_host: str = Field(default="")
    component_agent_broker_port: int = Field(default=443)
    component_agent_broker_scheme: str = Field(default="https")
    component_agent_broker_token: str = Field(default="")
