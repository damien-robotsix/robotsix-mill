"""Unit tests for text_utils.tail_keep (tail-keep truncation)."""

from robotsix_mill.core.text_utils import tail_keep


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
