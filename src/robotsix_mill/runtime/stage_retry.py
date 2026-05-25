"""Backoff helper for stage-level retries."""

import random


def compute_retry_delay(attempt: int, *, base: float, cap: float) -> float:
    """Compute exponential backoff delay with jitter.

    Args:
        attempt: 1-indexed attempt number.
        base: Base delay in seconds.
        cap: Maximum delay in seconds.

    Returns:
        Delay in seconds: ``min(cap, base * 2**(attempt-1)) + jitter``
        where jitter is uniform in ``[0, delay/2)``.
    """
    delay = min(cap, base * (2 ** (attempt - 1)))
    delay += random.uniform(0, delay / 2)
    return delay
