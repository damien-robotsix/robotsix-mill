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
    board_agent_api_url: str = Field(default="http://127.0.0.1:8077")
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

    # LLM board manager — conversational, natural-language board management. A
    # level-3 agent acts on the board (with a level-1 recall pass over its capped
    # question->answer memory). Reuses the board API + broker coords above and
    # mill's OpenRouter key; broker_token authenticates the manager agent. Off
    # by default.
    board_manager_enabled: bool = Field(default=False)
    board_manager_broker_token: str = Field(default="")
    board_manager_model: str = Field(default="")  # level-3; "" → tier default
    board_manager_recall_model: str = Field(default="")  # level-1; "" → default
    board_manager_max_conversations: int = Field(default=200)
    board_manager_max_concurrent: int = Field(
        default=1,
        description=(
            "Max concurrent board-manager Claude SDK runs. Bounded by a dedicated "
            "semaphore independent of claude_max_concurrency so board reads are never "
            "gated by heavy implement/audit/refine work. Default 1 matches the "
            "serial request-handling loop; increase only if robotsix-board-agent "
            "gains concurrent dispatch support."
        ),
    )
