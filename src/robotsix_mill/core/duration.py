"""Human-readable duration parsing/formatting — stdlib-only.

Periodic-agent run intervals are ultimately consumed downstream as an
integer number of seconds, but a raw ``86400`` carries no obvious
meaning to an operator editing a YAML file. This module lets intervals
be expressed in a compact, descending-order duration form::

    1w2d3h40m10s   →  790810 seconds
    1d             →  86400
    12h            →  43200
    90m            →  5400

Unit map: ``w`` = 604800s (week), ``d`` = 86400s (day), ``h`` = 3600s
(hour), ``m`` = 60s (minute), ``s`` = 1s (second). Each unit may appear
at most once, in descending order, with at least one unit present. A
bare integer (or integer-as-string) is accepted unchanged and
interpreted as seconds, so legacy raw-seconds YAML keeps working.

This module depends only on the stdlib (``re``) so it is importable
without pulling in the agent runtime, matching the ``core/`` convention.
"""

from __future__ import annotations

import re

# Unit → seconds, in canonical descending order (largest first). The
# insertion order matters: ``format_duration`` iterates it to emit the
# largest-to-smallest non-zero units.
_UNIT_SECONDS: dict[str, int] = {
    "w": 604800,
    "d": 86400,
    "h": 3600,
    "m": 60,
    "s": 1,
}

# Canonical descending-order duration: each unit optional, at most once.
# Matches the empty string too, so callers must verify at least one unit
# group is present.
_DURATION_RE = re.compile(
    r"^(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?"
    r"(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$"
)

_EXPECTED = "an integer number of seconds or a form like '1w2d3h40m10s'"


def parse_duration(value: str | int) -> int:
    """Parse *value* into a total number of seconds.

    An ``int`` passes through unchanged (interpreted as seconds);
    ``bool`` is rejected. A string in the canonical descending-order
    form ``1w2d3h40m10s`` (any subset of units, each at most once) is
    parsed to total seconds; a bare integer string (e.g. ``"3600"``) is
    accepted and treated as seconds. Surrounding whitespace is stripped.

    Raises ``ValueError`` for negative values or malformed input
    (empty string, unknown unit, duplicate unit, non-numeric magnitude,
    no units), with a message naming the offending value.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid duration {value!r}; expected {_EXPECTED}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"invalid duration {value!r}; must not be negative")
        return value

    text = value.strip()
    if not text:
        raise ValueError(f"invalid duration {value!r}; expected {_EXPECTED}")

    m = _DURATION_RE.match(text)
    if m is not None and any(m.group(u) is not None for u in _UNIT_SECONDS):
        total = 0
        for unit, mult in _UNIT_SECONDS.items():
            grp = m.group(unit)
            if grp is not None:
                total += int(grp) * mult
        return total

    # Bare integer string (keeps int-as-string YAML working).
    if re.fullmatch(r"\d+", text):
        return int(text)

    raise ValueError(f"invalid duration {value!r}; expected {_EXPECTED}")


def format_duration(seconds: int) -> str:
    """Render *seconds* as a compact duration string (inverse of
    :func:`parse_duration`).

    Emits only the largest-to-smallest non-zero units (``86400 -> "1d"``,
    ``9010 -> "2h30m10s"``); ``0`` renders as ``"0s"``. Raises
    ``ValueError`` for negative input. The round-trip property
    ``parse_duration(format_duration(n)) == n`` holds for any ``n >= 0``.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError(f"invalid duration {seconds!r}; expected a non-negative int")
    if seconds < 0:
        raise ValueError(f"invalid duration {seconds!r}; must not be negative")
    if seconds == 0:
        return "0s"

    parts: list[str] = []
    remaining = seconds
    for unit, mult in _UNIT_SECONDS.items():
        qty, remaining = divmod(remaining, mult)
        if qty:
            parts.append(f"{qty}{unit}")
    return "".join(parts)
