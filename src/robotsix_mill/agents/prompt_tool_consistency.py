"""Deterministic (no-LLM) guard: a prompt must not tell an agent to
*call* a tool the agent's resolved tool set doesn't include.

Two prompt/tool mismatches shipped and were point-fixed (PR #755, the
refine sendback override; PR #780, the implement review-feedback
injection). In each the agent's prompt instructed the LLM to *call* a
tool that the agent's resolved tool set never included, and nothing at
build time asserted that the two stayed in sync — so the divergence
surfaced only at runtime (a missing-tool error or a wasted operator
round-trip). This module is that missing assertion.

The check is intentionally narrow to avoid false positives. It flags
only an **imperative call directive** — a backtick-quoted tool name
immediately followed by a parenthesised call, e.g.
``call `close_thread(comment_id)```. A bare backtick-quoted mention
that is *not* a call — e.g. the disclaimer "you do not have a
`close_thread` tool" — is deliberately NOT flagged. The trailing ``(``
is what distinguishes the two.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# A tool name wrapped in one-or-more backticks (markdown `…` or RST
# ``…``) and immediately followed by a parenthesised call. The trailing
# ``(`` is what distinguishes an imperative *call* directive
# ("call `close_thread(comment_id)`") from a bare mention
# ("you do not have a `close_thread` tool").
_CALL_DIRECTIVE_RE = re.compile(r"`+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def call_directive_tools(prompt: str) -> set[str]:
    """Return the tool names that appear as ``call `<tool>(...)```
    directives in *prompt*.

    A bare backtick-quoted name with no following ``(`` (e.g. a
    disclaimer) is not a call directive and is not returned.
    """
    return set(_CALL_DIRECTIVE_RE.findall(prompt or ""))


def unregistered_call_directives(
    prompt: str,
    resolved_tools: Iterable[str],
    known_tools: Iterable[str] | None = None,
) -> set[str]:
    """Return the tools a prompt tells the agent to *call* that are
    absent from its resolved tool set.

    ``resolved_tools`` — names of the tools actually wired into the
    agent (e.g. ``[t.__name__ for t in handle's tools]``).

    ``known_tools`` — optional catalog of real mill tool names. When
    given, only call directives naming a known mill tool are
    considered, so an unrelated parenthesised backtick span (example
    code, ``cast(...)``, ``section(...)``) can't produce a false
    positive. When ``None`` every call directive is considered (the
    backtick-plus-parens pattern is the only guard).
    """
    resolved = set(resolved_tools)
    named = call_directive_tools(prompt)
    if known_tools is not None:
        catalog = set(known_tools)
        named = {n for n in named if n in catalog}
    return {n for n in named if n not in resolved}
