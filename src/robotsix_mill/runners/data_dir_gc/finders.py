"""Shared constants for the data-dir GC prune chain."""

from __future__ import annotations

import re

from ...core.states import State

# Lenient ticket-ID prefix check: only the leading timestamp is
# validated (``YYYYmmddTHHMMSSZ-``).
_TICKET_ID_PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z-")

# Maximum number of ticket IDs per SELECT ... WHERE id IN (...) batch.
_BATCH_SIZE = 500

# Terminal ticket states: those with empty outgoing transition sets.
_TERMINAL_STATES = {State.CLOSED, State.EPIC_CLOSED, State.ANSWERED}
