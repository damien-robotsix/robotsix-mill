"""Claude Agent SDK transient signatures, layered on the core set.

The SDK drives the ``claude`` CLI as a subprocess; a flaky spawn, a dropped
control-protocol connection, or a malformed JSON frame from the CLI is an
infrastructure hiccup that a re-run usually clears — treat those as transient.
"""

from __future__ import annotations

from ..core.retry import is_transient as _core_is_transient

# Subprocess/transport failures raised by claude_agent_sdk. Matched by type
# name so importing this module never requires the SDK to be installed.
_SDK_TRANSIENT_NAMES = {
    "CLIConnectionError",  # lost the control-protocol connection to the CLI
    "CLIJSONDecodeError",  # CLI emitted a malformed JSON frame
    "ProcessError",  # the CLI process exited non-zero
    "ProcessLookupError",  # the subprocess vanished mid-stream
}


def is_claude_sdk_transient(exc: BaseException) -> bool:
    """Core transient set OR a Claude Agent SDK subprocess/transport failure,
    walking the cause/context chain for the latter."""
    if _core_is_transient(exc):
        return True
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 10:
        if type(cur).__name__ in _SDK_TRANSIENT_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False
