"""Markdown-friendly section wrapper for user prompts.

Replaces the prior ``<ticket_spec>...</ticket_spec>`` XML-tag scaffolding
that rendered as escaped text in Langfuse's Formatted view (and added
zero structural value for human auditors). Sections are now framed by
a 4-backtick fenced code block with a language hint matching the
section name; a trailing HTML comment makes the close unambiguous to
the model and stays invisible in any Markdown viewer::

    ````ticket-spec
    ## Problem
    ...
    ````
    <!-- /ticket-spec -->

The 4-backtick outer fence is deliberate: the content (specs, diffs,
reviewer feedback) routinely embeds triple-backtick code blocks, so a
3-backtick wrapper would close on the first inner fence and corrupt
the block.

Convention: section names use ``kebab-case`` (a-z, hyphens) so the
language hint reads naturally as ``ticket-spec`` / ``git-diff`` /
``reviewer-feedback``.
"""

from __future__ import annotations


def section(name: str, content: str) -> str:
    """Wrap *content* in a 4-backtick fence with a closing comment marker.

    See module docstring for layout and rationale.
    """
    return f"````{name}\n{content}\n````\n<!-- /{name} -->"


__all__ = ["section"]
