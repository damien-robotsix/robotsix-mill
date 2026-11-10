"""Tests for the agent-pass runner's memory ledger I/O (load/persist/strip)."""

from pathlib import Path

from robotsix_mill.agents.runners.pass_runner import (
    _strip_unverified_filed_annotations,
    load_memory,
    persist_memory,
    strip_ephemeral_sections,
)
from robotsix_mill.config import Settings

# --- memory truncation tests (load_memory) ---


def test_load_memory_under_limit_noop(tmp_path):
    """When the memory file is ≤ max_chars, load_memory returns the
    full content unchanged."""
    mf = tmp_path / "memory.md"
    content = "## Entry 1\nSome content\n## Entry 2\nMore content\n"
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=8000)
    assert result == content


def test_load_memory_over_limit_truncates_keep_last(tmp_path, caplog):
    """When the file exceeds max_chars, load_memory keeps the LAST
    entries and prepends a truncation note."""
    import logging

    mf = tmp_path / "memory.md"
    # Build a memory file with dated sections, oldest first.
    sections = []
    for i in range(50):
        sections.append(f"## Entry {i}\nObservation {i}.\n" + ("x" * 200) + "\n")
    content = "\n".join(sections)
    mf.write_text(content, encoding="utf-8")
    assert len(content) > 8000

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=8000)

    # Must be ≤ max_chars + truncation note overhead (~60 chars)
    assert len(result) <= 8000 + 100
    # Must start with the truncation note
    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result.split("\n")[0]
    # Latest entries preserved (Entry 49 should be there)
    assert "Entry 49" in result
    # Earliest entries dropped
    assert "Entry 0" not in result
    # Warning logged
    assert "truncated:" in caplog.text


def test_load_memory_truncation_at_newline_boundary(tmp_path):
    """Truncation happens at a newline boundary so the first kept line
    is a complete line, not a fragment."""
    mf = tmp_path / "memory.md"
    # Build content with clear newline boundaries between entries.
    header = "## Entry old\n" + ("x" * 5000) + "\n"
    middle = "## Entry mid\n" + ("y" * 5000) + "\n"
    tail = "## Entry recent\nRecent observation.\n"
    content = header + middle + tail
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=3000)

    # The first line of the result (after the note) should NOT be a
    # mid-line fragment — it should start at a newline boundary.
    # Verify the note is present and the result contains the recent entry.
    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result.split("\n")[0]
    assert "## Entry recent" in result
    # The result should NOT contain a fragment like "xxxx" or "yyyy"
    # that is not on a complete line starting after a newline —
    # since we advance to the next newline after the cut point.


def test_load_memory_missing_file_ok(tmp_path):
    """load_memory on a non-existent file returns '' (existing behavior)."""
    mf = tmp_path / "nonexistent.md"
    result = load_memory(mf, max_chars=8000)
    assert result == ""


def test_load_memory_max_chars_none_no_truncation(tmp_path):
    """When max_chars is None, no truncation occurs (backward compat)."""
    mf = tmp_path / "memory.md"
    content = "x" * 12000
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=None)
    assert result == content


# --- config default ---


def test_max_memory_chars_default():
    """Bare Settings() has max_memory_chars == 8000."""
    assert Settings().max_memory_chars == 8000


# ------------------------------------------------------------------ persist_memory direct tests


def test_persist_memory_creates_new_file_and_parent_dirs(tmp_path):
    """persist_memory creates the file and parent directories when the
    memory_file does not exist."""
    memory_file = tmp_path / "sub" / "memory.md"
    assert not memory_file.exists()
    persist_memory(memory_file, "hello world")
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == "hello world"


def test_persist_memory_overwrites_existing_file(tmp_path):
    """persist_memory overwrites an existing file with new content."""
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("old content", encoding="utf-8")
    persist_memory(memory_file, "new content")
    assert memory_file.read_text(encoding="utf-8") == "new content"


def test_persist_memory_empty_text_file_exists_noop(tmp_path):
    """When text is empty and the file already exists, persist_memory
    does NOT overwrite it."""
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("original", encoding="utf-8")
    persist_memory(memory_file, "")
    assert memory_file.read_text(encoding="utf-8") == "original"


def test_persist_memory_empty_text_file_absent_creates_empty(tmp_path):
    """When text is empty and the file does NOT exist, persist_memory
    creates an empty file."""
    memory_file = tmp_path / "nonexistent.md"
    assert not memory_file.exists()
    persist_memory(memory_file, "")
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == ""


def test_persist_memory_oserror_swallowed(tmp_path, monkeypatch, caplog):
    """persist_memory swallows OSError and logs a warning."""
    import logging

    memory_file = tmp_path / "memory.md"

    def _failing_mkdir(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", _failing_mkdir)

    caplog.set_level(logging.WARNING)
    # Must not raise
    persist_memory(memory_file, "content")
    assert "could not write memory file" in caplog.text


# ------------------------------------------------------------------ load_memory additional edge cases


def test_load_memory_oserror_on_exists(tmp_path, monkeypatch, caplog):
    """OSError on Path.exists() returns '' and logs a warning."""
    import logging

    mf = tmp_path / "memory.md"
    # File does not exist, but exists() itself raises
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: (_ for _ in ()).throw(OSError("disk error")),
    )
    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=8000)
    assert result == ""
    assert "could not read memory file" in caplog.text


def test_load_memory_oserror_on_read_text(tmp_path, monkeypatch, caplog):
    """OSError on read_text() returns '' and logs a warning."""
    import logging

    mf = tmp_path / "memory.md"
    mf.write_text("some content", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, **kw: (_ for _ in ()).throw(OSError("read error")),
    )
    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=8000)
    assert result == ""
    assert "could not read memory file" in caplog.text


def test_load_memory_exact_max_chars_no_truncation(tmp_path, caplog):
    """File length exactly equals max_chars — no truncation, no warning."""
    import logging

    mf = tmp_path / "memory.md"
    content = "a" * 200
    mf.write_text(content, encoding="utf-8")

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=200)
    assert result == content
    # No warning about truncation
    assert "truncated" not in caplog.text


def test_load_memory_one_char_over_truncates(tmp_path, caplog):
    """File is 1 char over max_chars — truncation triggers."""
    import logging

    mf = tmp_path / "memory.md"
    content = "a" * 201  # 1 over
    mf.write_text(content, encoding="utf-8")

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=200)
    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result.split("\n")[0]
    assert "truncated" in caplog.text


def test_load_memory_single_line_over_limit(tmp_path, caplog):
    """File is one long line (no newline), exceeds max_chars.
    Fallback path: keeps text starting at cut_point (no newline to advance to)."""
    import logging

    mf = tmp_path / "memory.md"
    # One long line, no newline at all
    content = "x" * 500
    mf.write_text(content, encoding="utf-8")

    caplog.set_level(logging.WARNING)
    result = load_memory(mf, max_chars=200)
    assert result.startswith("[... memory truncated:")
    # The kept portion should not be empty
    kept_part = result.split("\n\n", 1)[1] if "\n\n" in result else ""
    assert len(kept_part) > 0


def test_load_memory_trailing_newline_over_limit(tmp_path):
    """File ends with newline, exceeds max_chars — kept portion starts
    on a complete line (not with a blank line from the trailing newline)."""
    mf = tmp_path / "memory.md"
    # Multiple entries, file ends with \n
    prefix = "## Old entry\n" + ("a" * 4000) + "\n"
    suffix = "## Recent entry\nRecent observation.\n"
    content = prefix + suffix
    mf.write_text(content, encoding="utf-8")

    result = load_memory(mf, max_chars=2000)
    assert result.startswith("[... memory truncated:")
    # The kept portion should include the recent entry on a proper line
    assert "## Recent entry" in result
    assert "Recent observation." in result
    # The kept text after the truncation note should NOT start with a
    # blank line caused by advancing past a trailing newline
    after_note = result.split("\n\n", 1)[1] if "\n\n" in result else result
    assert not after_note.startswith("\n")


# --- strip_ephemeral_sections (memory ledger hygiene) --------------


def test_strip_removes_prior_proposals_table():

    mem = (
        "## Project layout\n\nStages live in stages/.\n\n"
        "## Prior proposals — verified state\n\n"
        "| gap_id | ticket_id | state | resolution |\n"
        "|--------|-----------|-------|------------|\n"
        "| foo | 20260530Tc57b | CLOSED | merged |\n\n"
        "## Testing conventions\n\nUse pytest.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "Prior proposals" not in out
    assert "20260530Tc57b" not in out
    assert "## Project layout" in out
    assert "## Testing conventions" in out


def test_strip_table_at_end_of_memory():

    mem = "## Patterns\n\nfoo\n\n## Prior proposals — verified state\n\n| a | b |\n"
    out = strip_ephemeral_sections(mem)
    assert "Prior proposals" not in out
    assert "## Patterns" in out


def test_strip_noop_without_table():

    assert strip_ephemeral_sections("## Patterns\nfoo\n").strip() == (
        "## Patterns\nfoo"
    )
    assert strip_ephemeral_sections("") == ""


def test_strip_preserves_prose_after_table():
    """Only the heading + table rows are removed — trailing cross-ticket notes
    (no bounding ## heading) must survive (regression: a whole ledger got
    wiped to empty when prose followed the table)."""

    mem = (
        "## Prior proposals — verified state\n\n"
        "| gap_id | state |\n|--------|-------|\n| foo | CLOSED |\n\n"
        "Real cross-ticket pattern worth keeping.\nA thing to monitor.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "Prior proposals" not in out
    assert "foo | CLOSED" not in out
    assert "Real cross-ticket pattern" in out
    assert "thing to monitor" in out


def test_strip_removes_recent_proposals_block_only_content():
    """A standalone ``<recent_proposals>`` block is stripped even when it is
    the ONLY ephemeral content (exercises the updated fast-path guard that no
    longer short-circuits on the XML-block-only case)."""

    mem = (
        "<recent_proposals>\n"
        "[DRAFT] 20260530Tc57b | Split big module\n"
        "[DONE] 20260530Td88a | Add tests\n"
        "</recent_proposals>\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "<recent_proposals>" not in out
    assert "20260530Tc57b" not in out
    assert out == ""


def test_strip_recent_proposals_preserves_surrounding_prose():
    """The echoed ``<recent_proposals>`` block is removed but the
    cross-ticket prose around it survives."""

    mem = (
        "## Patterns\n\nReal observation worth keeping.\n\n"
        "<recent_proposals>\n[DRAFT] 20260530Tc57b | Split big module\n"
        "</recent_proposals>\n\n"
        "Another thing to monitor.\n"
    )
    out = strip_ephemeral_sections(mem)
    assert "<recent_proposals>" not in out
    assert "20260530Tc57b" not in out
    assert "## Patterns" in out
    assert "Real observation worth keeping" in out
    assert "Another thing to monitor" in out


def test_strip_recent_proposals_noop_without_block():
    """Memory with no ephemeral content (no table, no XML block) is returned
    unchanged."""

    assert strip_ephemeral_sections("## Patterns\nfoo\n") == "## Patterns\nfoo\n"


def test_persist_memory_strips_echoed_recent_proposals(tmp_path):
    """An echoed ``<recent_proposals>`` block never lands in the written
    ledger file."""

    memory_file = tmp_path / "memory.md"
    persist_memory(
        memory_file,
        "## Patterns\n\nKeep this.\n\n"
        "<recent_proposals>\n[DRAFT] 20260530Tc57b | Split\n"
        "</recent_proposals>\n",
    )
    written = memory_file.read_text(encoding="utf-8")
    assert "<recent_proposals>" not in written
    assert "20260530Tc57b" not in written
    assert "Keep this." in written


# ------------------------------------------------------------------ persist_memory max_chars (write-side trimming)


def test_persist_memory_trims_when_over_max_chars(tmp_path, caplog):
    """When max_chars is set and text exceeds it, the persisted file is
    trimmed to ≤ max_chars + note overhead, newest entries preserved,
    truncation note prepended."""
    import logging

    memory_file = tmp_path / "memory.md"
    # Build a text with clear dated sections — oldest first, newest last.
    sections = []
    for i in range(50):
        sections.append(f"## Entry {i}\nObservation {i}.\n" + ("x" * 200) + "\n")
    text = "\n".join(sections)
    assert len(text) > 8000

    caplog.set_level(logging.WARNING)
    persist_memory(memory_file, text, max_chars=8000)

    written = memory_file.read_text(encoding="utf-8")
    # Must be ≤ max_chars + truncation note overhead (~60 chars)
    assert len(written) <= 8000 + 100
    # Truncation note present
    assert written.startswith("[... memory truncated:")
    assert "chars omitted]" in written.split("\n")[0]
    # Latest entries preserved (Entry 49)
    assert "Entry 49" in written
    # Earliest entries dropped
    assert "Entry 0" not in written
    # Warning logged (same pattern as load_memory)
    assert "truncated on write:" in caplog.text


def test_persist_memory_noop_when_under_max_chars(tmp_path, caplog):
    """When text is under max_chars, it is written unchanged — no
    truncation note, no warning."""
    import logging

    memory_file = tmp_path / "memory.md"
    text = "## Only entry\nShort observation.\n"
    assert len(text) < 8000

    caplog.set_level(logging.WARNING)
    persist_memory(memory_file, text, max_chars=8000)

    written = memory_file.read_text(encoding="utf-8")
    assert written == text
    # No truncation warning
    assert "truncated" not in caplog.text


def test_persist_memory_max_chars_none_backward_compat(tmp_path, caplog):
    """max_chars=None (the default) writes the full text without trimming."""
    import logging

    memory_file = tmp_path / "memory.md"
    text = "x" * 12000  # well over any typical cap
    assert len(text) > 8000

    caplog.set_level(logging.WARNING)
    persist_memory(memory_file, text, max_chars=None)

    written = memory_file.read_text(encoding="utf-8")
    assert written == text
    assert "truncated" not in caplog.text


def test_persist_memory_max_chars_default_none_backward_compat(tmp_path, caplog):
    """Calling persist_memory without max_chars (positional only)
    writes full text — backward compatible with existing callers."""
    import logging

    memory_file = tmp_path / "memory.md"
    text = "x" * 12000
    assert len(text) > 8000

    caplog.set_level(logging.WARNING)
    persist_memory(memory_file, text)

    written = memory_file.read_text(encoding="utf-8")
    assert written == text
    assert "truncated" not in caplog.text


def test_persist_memory_still_strips_ephemeral_with_max_chars(tmp_path):
    """Even when max_chars triggers truncation, ephemeral sections are
    still stripped BEFORE the truncation check. The tail_keep after
    stripping preserves the newest content."""
    memory_file = tmp_path / "memory.md"
    # Build text with a long prefix (oldest), an ephemeral table in the
    # middle, and a short tail with the newest marker content. After
    # stripping the ephemeral table, the remaining text is still over
    # max_chars because of the long prefix — tail_keep drops the old
    # prefix and preserves the newest tail marker.
    old_prefix = "## Old prefix\n" + ("z " * 3000) + "\n"
    ephemeral_part = (
        "## Prior proposals — verified state\n\n"
        "| gap_id | ticket_id | state | resolution |\n"
        "|--------|-----------|-------|------------|\n"
        "| foo | T-1 | CLOSED | merged |\n\n"
    )
    tail_marker = "## Patterns\nRecent observation — keep this.\n"
    text = old_prefix + ephemeral_part + tail_marker

    persist_memory(memory_file, text, max_chars=4000)
    written = memory_file.read_text(encoding="utf-8")
    # Ephemeral table must not appear
    assert "Prior proposals" not in written
    assert "T-1" not in written
    # The tail marker (newest content) should be preserved
    assert "## Patterns" in written
    assert "Recent observation — keep this." in written
    # The old prefix (oldest content) should be dropped
    assert "## Old prefix" not in written


# ==================================================================
# persist_memory truncation tests (write-side max_chars)
# ==================================================================


def test_persist_memory_under_limit_no_truncation(tmp_path):
    """Text ≤ max_chars is written unchanged."""
    memory_file = tmp_path / "memory.md"
    content = "## Entry 1\nObservation.\n## Entry 2\nMore observation.\n"
    persist_memory(memory_file, content, max_chars=8000)
    assert memory_file.read_text(encoding="utf-8") == content


def test_persist_memory_over_limit_tail_truncates(tmp_path, caplog):
    """Text > max_chars is tail-truncated: oldest content dropped,
    truncation note prepended."""
    import logging

    memory_file = tmp_path / "memory.md"
    # Build content with chronological entries, oldest first.
    sections = []
    for i in range(50):
        sections.append(f"## Entry {i}\nObservation {i}.\n" + ("x" * 200) + "\n")
    content = "\n".join(sections)
    assert len(content) > 8000

    caplog.set_level(logging.WARNING)
    persist_memory(memory_file, content, max_chars=8000)

    written = memory_file.read_text(encoding="utf-8")
    # Must be ≤ max_chars + truncation note overhead (~60 chars)
    assert len(written) <= 8000 + 100
    # Must start with the truncation note
    assert written.startswith("[... memory truncated:")
    assert "chars omitted]" in written.split("\n")[0]
    # Latest entries preserved
    assert "Entry 49" in written
    # Earliest entries dropped
    assert "Entry 0" not in written
    # Warning logged
    assert "truncated on write" in caplog.text


def test_persist_memory_max_chars_none_no_truncation(tmp_path):
    """max_chars=None (default) writes full text unchanged (backward compat)."""
    memory_file = tmp_path / "memory.md"
    content = "x" * 12000
    persist_memory(memory_file, content, max_chars=None)
    assert memory_file.read_text(encoding="utf-8") == content


def test_persist_memory_max_chars_none_default_parameter(tmp_path):
    """max_chars omitted (default) writes full text unchanged."""
    memory_file = tmp_path / "memory.md"
    content = "x" * 12000
    persist_memory(memory_file, content)  # max_chars NOT passed
    assert memory_file.read_text(encoding="utf-8") == content


def test_persist_memory_truncation_after_strip_ephemeral(tmp_path):
    """Truncation happens AFTER ephemeral sections are stripped.
    Ephemeral content doesn't waste the cap budget."""
    memory_file = tmp_path / "memory.md"
    # Build content where the ephemeral sections take up ~500 chars
    # and the real entries + padding take up the rest.
    # After stripping, the real content should fit under the cap.
    real_lines = ["## Real entry\nObservation that matters.\n"]
    for _i in range(60):
        real_lines.append("z" * 80)
    real_content = "\n".join(real_lines)  # ~4900 chars
    ephemeral = "\n\n## Prior proposals — verified state\n\n" + "\n".join(
        f"| gap_{i} | T-{1000 + i} | CLOSED | merged |" for i in range(20)
    )  # ~1000 chars
    content = real_content + ephemeral
    assert len(content) > 5000

    persist_memory(memory_file, content, max_chars=6000)

    written = memory_file.read_text(encoding="utf-8")
    # Ephemeral sections must NOT be in the written file
    assert "Prior proposals" not in written
    assert "gap_0" not in written
    # The real content must be fully preserved (under the cap after strip)
    assert "## Real entry" in written
    assert "Observation that matters" in written
    # No truncation note should appear (real content fits under cap)
    assert "[... memory truncated:" not in written


def test_persist_memory_still_truncates_after_strip_when_still_over(tmp_path):
    """When content is still over max_chars after stripping ephemeral
    sections, truncation still applies."""
    memory_file = tmp_path / "memory.md"
    # Build real content structured as chronological entries (oldest
    # first).  max_chars=500, post-strip content ~6000 chars.
    # The tail should contain the "recent" marker lines.
    oldest = "## Old entry\n" + ("a" * 5500) + "\n"
    recent = "## Recent entry\nObservation that must survive.\n"
    real_content = oldest + recent
    # Add ephemeral that will be stripped.
    ephemeral = (
        "\n\n## Prior proposals — verified state\n\n"
        "| gap_id | ticket_id | state | resolution |\n"
        "|--------|-----------|-------|------------|\n"
        "| gap_1 | T-1 | CLOSED | merged |\n"
    )
    content = real_content + ephemeral

    persist_memory(memory_file, content, max_chars=500)

    written = memory_file.read_text(encoding="utf-8")
    # Ephemeral stripped
    assert "Prior proposals" not in written
    # Truncation applied on the remaining real content
    assert written.startswith("[... memory truncated:")
    assert "chars omitted]" in written.split("\n")[0]
    # The tail (most recent) content is preserved.  "## Recent entry"
    # is at the very end of the real content so tail_keep preserves it.
    assert "## Recent entry" in written
    assert "Observation that must survive" in written


def test_persist_memory_exact_max_chars_no_truncation(tmp_path):
    """Content length exactly equals max_chars — no truncation."""
    memory_file = tmp_path / "memory.md"
    content = "a" * 200
    persist_memory(memory_file, content, max_chars=200)
    assert memory_file.read_text(encoding="utf-8") == content


def test_persist_memory_one_char_over_truncates(tmp_path):
    """Content is 1 char over max_chars — truncation triggers."""
    memory_file = tmp_path / "memory.md"
    content = "a" * 201
    persist_memory(memory_file, content, max_chars=200)
    written = memory_file.read_text(encoding="utf-8")
    assert written.startswith("[... memory truncated:")
    assert "chars omitted]" in written.split("\n")[0]


def test_persist_memory_empty_text_noop_regardless_of_max_chars(tmp_path):
    """Empty text is always a no-op, even with max_chars set."""
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("original", encoding="utf-8")
    persist_memory(memory_file, "", max_chars=8000)
    assert memory_file.read_text(encoding="utf-8") == "original"


def test_persist_memory_creates_parent_dirs_with_truncation(tmp_path):
    """Parent directory creation still works when truncation is active."""
    memory_file = tmp_path / "sub" / "deep" / "memory.md"
    assert not memory_file.exists()
    content = "x" * 5000
    persist_memory(memory_file, content, max_chars=2000)
    assert memory_file.exists()
    written = memory_file.read_text(encoding="utf-8")
    assert written.startswith("[... memory truncated:")
    assert len(written) <= 2000 + 100


# ------------------------------------------------------------------
# _strip_unverified_filed_annotations
# ------------------------------------------------------------------


def test_strip_unverified_filed_annotations_empty_verified_ids():
    """When verified_gap_ids is empty, ALL 'Filed' annotations are stripped
    because none of them were verified as filed."""
    memory = "- gap-foo — Filed this run\nOther text\n"
    result = _strip_unverified_filed_annotations(memory, [])
    # The 'Filed' annotation line is stripped; non-annotation lines survive.
    assert "gap-foo" not in result
    assert "Other text" in result


def test_strip_unverified_filed_annotations_empty_memory():
    """Empty memory string returns empty string."""
    result = _strip_unverified_filed_annotations("", ["gap-foo"])
    assert result == ""


def test_strip_unverified_filed_annotations_none_memory():
    """None-like (empty string) memory is returned as-is."""
    result = _strip_unverified_filed_annotations("", [])
    assert result == ""


def test_strip_unverified_filed_annotations_keeps_verified():
    """Lines with gap_ids in verified list are preserved."""
    memory = "- gap-foo — Filed this run\n- gap-bar — Filed this run\n"
    result = _strip_unverified_filed_annotations(memory, ["gap-foo", "gap-bar"])
    assert "- gap-foo — Filed this run" in result
    assert "- gap-bar — Filed this run" in result


def test_strip_unverified_filed_annotations_removes_unverified():
    """Lines with gap_ids NOT in verified list are stripped."""
    memory = (
        "- gap-foo — Filed this run\n"
        "- gap-unverified — Filed this run\n"
        "- gap-bar — Filed this run\n"
    )
    result = _strip_unverified_filed_annotations(memory, ["gap-foo", "gap-bar"])
    assert "- gap-foo — Filed this run" in result
    assert "- gap-bar — Filed this run" in result
    assert "gap-unverified" not in result


def test_strip_unverified_filed_annotations_lowercase_filed():
    """Case-insensitive match for 'filed'."""
    memory = "- gap-foo — filed this run\n"
    result = _strip_unverified_filed_annotations(memory, [])
    assert "gap-foo" not in result


def test_strip_unverified_filed_annotations_emdash():
    """Matches em-dash separator."""
    memory = "- gap-foo — Filed this run\n"
    result = _strip_unverified_filed_annotations(memory, [])
    assert "gap-foo" not in result


def test_strip_unverified_filed_annotations_endash():
    """Matches en-dash separator."""
    memory = "- gap-foo – Filed this run\n"
    result = _strip_unverified_filed_annotations(memory, [])
    assert "gap-foo" not in result


def test_strip_unverified_filed_annotations_preserves_non_annotation_lines():
    """Lines that don't match the 'filed' pattern are left untouched."""
    memory = (
        "## Proposals\nSome prose about gaps\n- gap-foo — Filed this run\nMore text\n"
    )
    result = _strip_unverified_filed_annotations(memory, ["gap-foo"])
    assert "## Proposals" in result
    assert "Some prose about gaps" in result
    assert "- gap-foo — Filed this run" in result
    assert "More text" in result


def test_strip_unverified_filed_annotations_no_filed_annotations():
    """Memory with no 'Filed' annotations is returned unchanged."""
    memory = "## Proposals\n- gap-foo: observed, not filed yet\n"
    result = _strip_unverified_filed_annotations(memory, ["gap-foo"])
    assert result == memory
