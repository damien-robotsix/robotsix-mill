"""Unit tests for text_utils.tail_keep (tail-keep truncation)."""

from robotsix_mill.core.text_utils import head_tail_keep, html_to_text, tail_keep


def test_under_limit_returns_unchanged():
    text = "line one\nline two\nline three\n"
    assert tail_keep(text, 10_000) == text


def test_exact_limit_returns_unchanged():
    text = "abcde"
    assert tail_keep(text, len(text)) == text


def test_over_limit_keeps_tail_with_note():
    lines = [f"entry {i:03d}" for i in range(500)]
    text = "\n".join(lines) + "\n"
    result = tail_keep(text, 200, label="memory")

    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result
    # Most-recent content survives; oldest dropped.
    assert "entry 499" in result
    assert "entry 000" not in result


def test_truncation_at_newline_boundary():
    # 5 lines of 100 'x' chars each, newline-terminated.
    text = "\n".join("x" * 100 for _ in range(5)) + "\n"
    result = tail_keep(text, 150)
    body = result.split("\n\n", 1)[1]
    # Every kept line should be a complete 100-char line.
    for line in body.splitlines():
        assert len(line) == 100


def test_default_label():
    text = "a" * 50 + "\n" + "b" * 50 + "\n"
    result = tail_keep(text, 30)
    assert result.startswith("[... content truncated:")


# --- head_tail_keep (middle truncation) -------------------------------------


def test_head_tail_under_limit_returns_unchanged():
    text = "line one\nline two\nline three\n"
    assert head_tail_keep(text, 10_000) == text


def test_head_tail_zero_cap_returns_unchanged():
    text = "a" * 10_000
    assert head_tail_keep(text, 0) == text


def test_head_tail_over_limit_keeps_head_and_tail_with_marker():
    # Distinct head and tail lines so we can assert both survive.
    head_lines = "\n".join(f"HEAD-{i:04d}" for i in range(2000))
    tail_lines = "\n".join(f"TAIL-{i:04d}" for i in range(2000))
    text = head_lines + "\n" + tail_lines + "\n"

    max_chars = 4000
    result = head_tail_keep(text, max_chars, label="git-diff")

    # Marker line present and labelled.
    assert "[... git-diff truncated:" in result
    assert "omitted from the middle" in result
    # Both early and late content represented.
    assert "HEAD-0000" in result
    assert "TAIL-1999" in result
    # The middle is dropped — some interior lines are gone.
    assert "HEAD-1500" not in result
    # Length bounded by max_chars plus the marker line.
    marker_overhead = len(
        "\n\n[... git-diff truncated: 999999 chars omitted from the middle ...]\n\n"
    )
    assert len(result) <= max_chars + marker_overhead


def test_head_tail_kept_lines_are_complete():
    text = "\n".join("x" * 100 for _ in range(200)) + "\n"
    result = head_tail_keep(text, 1500)
    head_part, rest = result.split("\n\n[... ", 1)
    tail_part = rest.split("...]\n\n", 1)[1]
    for line in head_part.splitlines():
        assert len(line) == 100
    for line in tail_part.splitlines():
        if line:
            assert len(line) == 100


# --- html_to_text helper ----------------------------------------------------


def test_html_to_text_drops_scripts_and_styles():
    """Scripts and styles are removed wholesale (content + tags) so
    an LLM doesn't have to read JavaScript or CSS. They're dead
    weight in every doc page we fetch."""
    body = (
        "<html><body>"
        "<script>alert('x'); var leaked = 'data';</script>"
        "<style>body { color: red; }</style>"
        "<p>Hello world</p>"
        "</body></html>"
    )
    out = html_to_text(body)
    assert "alert" not in out
    assert "leaked" not in out
    assert "color: red" not in out
    assert "Hello world" in out


def test_html_to_text_unescapes_entities():
    """``&amp;`` → ``&`` and ``&nbsp;`` → space — the agent reads the
    rendered text, not the source-level entity references."""
    out = html_to_text("<p>foo &amp; bar&nbsp;baz</p>")
    assert "&" in out
    # &nbsp; came through as a real space; the result has no
    # entity reference text.
    assert "&nbsp;" not in out
    assert "foo & bar" in out


def test_html_to_text_collapses_whitespace():
    """Removing tags inserts runs of whitespace. The extractor
    collapses them so the agent doesn't see paragraphs of newlines
    between every word."""
    body = "<div><p>one</p>\n\n\n<p>two</p></div>"
    out = html_to_text(body)
    # At most one blank line between paragraphs.
    assert "\n\n\n" not in out
    assert "one" in out
    assert "two" in out
