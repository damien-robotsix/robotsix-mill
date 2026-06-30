"""Tests for ``robotsix_mill.core.delta_context`` — draft trimming."""

from __future__ import annotations

from robotsix_mill.core.delta_context import (
    trim_large_artifacts,
    trim_spec_for_retry,
    trim_draft_for_re_refine,
)


class TestTrimSpecForRetry:
    def test_draft_below_max_chars_unchanged(self):
        spec = "short spec"
        assert trim_spec_for_retry(spec, max_chars=800) == spec

    def test_draft_above_max_truncates_at_paragraph_boundary(self):
        spec = "First paragraph\n\n" + ("line " * 300)
        result = trim_spec_for_retry(spec, max_chars=800)
        assert "spec truncated" in result
        assert len(result) < len(spec)

    def test_draft_no_paragraph_boundary_falls_back_to_newline(self):
        spec = "First line\n" + ("line " * 300)
        result = trim_spec_for_retry(spec, max_chars=800)
        assert "spec truncated" in result


class TestTrimDraftForReRefine:
    def test_delegates_to_trim_spec_for_retry(self):
        spec = "draft content\n\n" + ("padding " * 100)
        result = trim_draft_for_re_refine(spec, max_chars=800)
        assert "spec truncated" in result


# ---------------------------------------------------------------------------
# trim_large_artifacts — lockfile / CI-log trimming
# ---------------------------------------------------------------------------


class TestTrimLargeArtifacts:
    """``trim_large_artifacts`` trims lockfile diffs and CI log dumps."""

    def test_small_draft_passes_through(self):
        """Drafts ≤ _TRIM_MIN_CHARS are returned unchanged."""
        draft = "a small draft\n" * 5
        assert trim_large_artifacts(draft) == draft

    def test_no_signal_passes_through(self):
        """A large draft without lockfile or CI-log signals is unchanged."""
        draft = "ordinary prose text\n" * 500  # well over 4000 chars
        assert trim_large_artifacts(draft) == draft

    def test_lockfile_diff_trimmed(self):
        """A lockfile diff block exceeding 50 lines is summarised."""
        header = "diff --git a/uv.lock b/uv.lock\nindex abc..def 100644\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1,5 +1,10 @@\n"
        lock_lines = "  line " + "\n  line ".join(str(i) for i in range(1, 100))
        padding = "ordinary prose text\n" * 200  # push past _TRIM_MIN_CHARS
        draft = padding + "\n" + header + lock_lines
        result = trim_large_artifacts(draft)
        assert "lines of lockfile diff omitted" in result
        assert "draft-original.md" in result

    def test_small_lockfile_diff_kept(self):
        """A lockfile diff ≤ 50 lines is kept unchanged."""
        header = "diff --git a/uv.lock b/uv.lock\nindex abc..def 100644\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1,3 +1,5 @@\n"
        lock_lines = "\n".join(f"  line {i}" for i in range(1, 10))  # 9 lines
        padding = "ordinary prose text\n" * 200
        draft = padding + "\n" + header + lock_lines
        result = trim_large_artifacts(draft)
        # The lockfile diff is small enough to be retained.
        assert "lines of lockfile diff omitted" not in result
        assert "uv.lock" in result

    def test_ci_log_trimmed(self):
        """A CI log dump block is summarised."""
        padding = "ordinary prose text\n" * 200
        ci_block = "= FAILURES =\n" + "test_x failed\n" * 250
        draft = padding + "\n\n" + ci_block
        result = trim_large_artifacts(draft)
        assert "CI log output truncated" in result
        assert "draft-original.md" in result

    def test_ci_log_small_block_kept(self):
        """A small CI log block below the char threshold is kept."""
        padding = "ordinary prose text\n" * 200
        ci_block = "= FAILURES =\ntest_x failed\n"  # small
        draft = padding + "\n\n" + ci_block
        result = trim_large_artifacts(draft)
        assert "CI log output truncated" not in result

    def test_package_lock_json_trimmed(self):
        """package-lock.json diffs are also trimmed."""
        header = "diff --git a/package-lock.json b/package-lock.json\nindex abc..def 100644\n--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1,5 +1,10 @@\n"
        lock_lines = "  line " + "\n  line ".join(str(i) for i in range(1, 100))
        padding = "ordinary prose text\n" * 200
        draft = padding + "\n" + header + lock_lines
        result = trim_large_artifacts(draft)
        assert "lines of lockfile diff omitted" in result

    def test_unknown_file_not_trimmed(self):
        """A diff for a non-lockfile file is not trimmed."""
        header = "diff --git a/src/main.py b/src/main.py\nindex abc..def 100644\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1,5 +1,10 @@\n"
        lines = "  line " + "\n  line ".join(str(i) for i in range(1, 100))
        padding = "ordinary prose text\n" * 200
        draft = padding + "\n" + header + lines
        result = trim_large_artifacts(draft)
        assert "lines of lockfile diff omitted" not in result
        assert "src/main.py" in result  # preserved

    def test_ci_log_with_lockfile_both_trimmed(self):
        """When both lockfile and CI-log signals are present, both are trimmed."""
        header = "diff --git a/uv.lock b/uv.lock\nindex abc..def 100644\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1,5 +1,10 @@\n"
        lock_lines = "  line " + "\n  line ".join(str(i) for i in range(1, 100))
        padding = "ordinary prose text\n" * 200
        ci_block = "= FAILURES =\n" + "test_x failed\n" * 250
        draft = padding + "\n" + header + lock_lines + "\n\n" + ci_block
        result = trim_large_artifacts(draft)
        assert "lines of lockfile diff omitted" in result
        assert "CI log output truncated" in result
