"""Component-agent responder for robotsix-mill.

This package implements the generic monitor / config-get / config-set
component contract over the agent-comm broker. It is additive to the
existing BrokeredBoardResponder / BoardManager integration — those are
untouched.

Imports from this package do NOT contact the broker. All broker
interaction is deferred to ``ComponentAgentResponder.start()``.
"""

from __future__ import annotations
