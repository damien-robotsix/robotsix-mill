"""Tests for the per-repo agent-overlay loader.

Overlays let a managed repo carry repo-specific prompt guidance for
mill's generic periodic agents in its own source tree at
``<repo>/.robotsix-mill/agent_overlays/<agent>.md``.

The loader is the shared seam every generic periodic agent uses, so
its contract has to be tight: missing files MUST be silent (a repo
with no overlays behaves exactly as before), empty repos MUST be
no-ops, and the file's contents MUST be returned verbatim (stripped)
so operator-authored Markdown isn't mangled.
"""

from __future__ import annotations


from robotsix_mill.agents.overlays import apply_overlay, load_overlay


class TestLoadOverlay:
    def test_returns_empty_when_repo_dir_is_none(self):
        """No clone available → no overlay. Generic periodic agents
        that can run without a clone (rare but possible) must not
        crash; they just see the shipped prompt."""
        assert load_overlay(None, "audit") == ""

    def test_returns_empty_when_overlay_dir_missing(self, tmp_path):
        """A clone without a .robotsix-mill/ folder behaves identically
        to one without an overlay file — silent no-op."""
        assert load_overlay(tmp_path, "audit") == ""

    def test_returns_empty_when_overlay_file_missing(self, tmp_path):
        """The .robotsix-mill/ dir exists but no overlay for this
        agent. Other agents may carry overlays; this one shouldn't
        accidentally pick them up."""
        (tmp_path / ".robotsix-mill" / "agent_overlays").mkdir(parents=True)
        (tmp_path / ".robotsix-mill" / "agent_overlays" / "health.md").write_text(
            "health-only guidance"
        )
        assert load_overlay(tmp_path, "audit") == ""

    def test_returns_file_contents_stripped(self, tmp_path):
        """File content is returned with leading/trailing whitespace
        removed but interior formatting preserved verbatim. Operators
        write Markdown; we don't rewrap or normalise it."""
        overlay_dir = tmp_path / ".robotsix-mill" / "agent_overlays"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "audit.md").write_text(
            "\n\n## repo-specific\n\nFocus on FastAPI route hygiene.\n\n"
        )
        assert (
            load_overlay(tmp_path, "audit")
            == "## repo-specific\n\nFocus on FastAPI route hygiene."
        )

    def test_keyed_by_agent_name(self, tmp_path):
        """Each agent reads its own overlay file; one repo can carry
        guidance for many agents without cross-talk."""
        overlay_dir = tmp_path / ".robotsix-mill" / "agent_overlays"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "audit.md").write_text("AUDIT context")
        (overlay_dir / "health.md").write_text("HEALTH context")
        assert load_overlay(tmp_path, "audit") == "AUDIT context"
        assert load_overlay(tmp_path, "health") == "HEALTH context"


class TestApplyOverlay:
    def test_empty_overlay_returns_prompt_unchanged(self):
        """A repo with no overlay must not add separator whitespace —
        agents that build deterministic prompt-hashes (for caching or
        tests) depend on byte-identical output to the shipped YAML."""
        assert apply_overlay("system", "") == "system"

    def test_overlay_appended_with_blank_line_separator(self):
        """The overlay is appended after a blank line and followed by
        a trailing newline, so it parses as its own Markdown section
        and the next user-prompt block starts cleanly."""
        out = apply_overlay("system", "extra")
        assert out == "system\n\nextra\n"
