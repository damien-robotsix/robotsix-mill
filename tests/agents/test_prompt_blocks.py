"""Tests for :mod:`robotsix_mill.agents.prompt_blocks`.

``section()`` is a pure string-wrapping utility with no I/O, no
imports from the rest of the repo, and no branching. The contract
that matters: the 4-backtick outer fence survives nested triple
backticks in *content*, and the closing ``<!-- /name -->`` comment
trails the fence so the model has an unambiguous end-marker.
"""

from __future__ import annotations

import pytest

from robotsix_mill.agents.prompt_blocks import section


# ---------------------------------------------------------------------------
# Basic wrapping — the canonical shape from the module docstring
# ---------------------------------------------------------------------------


def test_section_canonical_shape():
    """``section('ticket-spec', '## Problem\\n...')`` produces the exact
    4-backtick fenced block + trailing close-comment shown in the
    module docstring."""
    out = section("ticket-spec", "## Problem\n...")
    assert out == "````ticket-spec\n## Problem\n...\n````\n<!-- /ticket-spec -->"


def test_section_starts_with_four_backticks_and_name():
    """The opening fence is four backticks followed by the section
    name (the language hint), then a newline."""
    out = section("git-diff", "diff --git a/x b/x\n")
    assert out.startswith("````git-diff\n")


def test_section_ends_with_close_comment():
    """The trailing ``<!-- /name -->`` comment is mandatory; it gives
    the model an unambiguous end-marker that stays invisible to any
    Markdown viewer."""
    out = section("reviewer-feedback", "looks fine")
    assert out.endswith("````\n<!-- /reviewer-feedback -->")


# ---------------------------------------------------------------------------
# Triple-backtick safety — the load-bearing rationale for 4-backtick wrappers
# ---------------------------------------------------------------------------


def test_section_survives_nested_triple_backticks():
    """The 4-backtick outer fence MUST NOT be closed by a 3-backtick
    code-fence inside *content* — otherwise specs / diffs / reviewer
    feedback that embed code blocks would corrupt the wrapper."""
    body = "Here is a code block:\n```python\nprint('hi')\n```\nand more text"
    out = section("ticket-spec", body)
    # The inner triple-backticks are preserved verbatim.
    assert "```python\nprint('hi')\n```" in out
    # The wrapper still ends with its own 4-backtick fence + comment.
    assert out.endswith("````\n<!-- /ticket-spec -->")
    # No spurious 4-backtick sequence appeared mid-content.
    assert out.count("````") == 2


def test_section_survives_nested_four_backtick_marker_in_content():
    """A ``````x`````` substring inside *content* is rendered verbatim;
    the function is intentionally a thin format — the caller is
    responsible for picking a name that does not collide. This test
    pins the (non-)escaping behaviour."""
    body = "Embedded: ````note\nhi\n````"
    out = section("ticket-spec", body)
    # Content is preserved verbatim — no escaping.
    assert body in out


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, content, expected",
    [
        # empty content
        ("ticket-spec", "", "````ticket-spec\n\n````\n<!-- /ticket-spec -->"),
        # empty name
        ("", "body", "````\nbody\n````\n<!-- / -->"),
        # both empty
        ("", "", "````\n\n````\n<!-- / -->"),
    ],
)
def test_section_empty_inputs(name, content, expected):
    """Empty ``name`` and/or ``content`` produce the same shape (no
    validation). The caller decides what is sensible."""
    assert section(name, content) == expected


def test_section_content_containing_close_marker_is_not_escaped():
    """``section()`` does not escape the literal closing pattern
    inside *content* — if a caller actually embeds ``<!-- /name -->``
    in their body, that string passes through verbatim. The function
    is a thin format, not a sanitiser."""
    body = "Some text\n<!-- /ticket-spec -->\nmore text"
    out = section("ticket-spec", body)
    assert body in out
    # Real closing marker is still last.
    assert out.endswith("\n<!-- /ticket-spec -->")


def test_section_multi_line_content_preserves_newlines_verbatim():
    """Multi-line *content* is preserved verbatim (no normalisation,
    no stripping); only one ``\\n`` is added before the closing fence."""
    body = "line 1\nline 2\n\nline 4"
    out = section("git-diff", body)
    assert "\nline 1\nline 2\n\nline 4\n" in out


@pytest.mark.parametrize(
    "name",
    ["ticket-spec", "git-diff", "reviewer-feedback", "code-review"],
)
def test_section_kebab_case_names_pass_through(name):
    """Kebab-case names — the documented convention — pass through
    unchanged into the opening fence."""
    out = section(name, "body")
    assert out.startswith(f"````{name}\n")
    assert out.endswith(f"````\n<!-- /{name} -->")


def test_section_name_with_spaces_is_accepted_unchanged():
    """``section()`` performs no name validation; weird names with
    spaces pass through (the caller picks them, the caller owns the
    consequence)."""
    out = section("not kebab", "body")
    assert out.startswith("````not kebab\n")
    assert out.endswith("````\n<!-- /not kebab -->")


def test_section_unicode_content():
    """Unicode body content passes through unchanged."""
    body = "héllo — café 🎉 日本語"
    out = section("ticket-spec", body)
    assert body in out
