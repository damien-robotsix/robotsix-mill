"""Tests for the ``insert_changelog_entry`` agent tool.

The tool replaces the LLM-prompt-driven ``edit_file`` approach to
CHANGELOG.md insertion, which had a ≈42% corruption rate when the
existing top entry spanned multiple continuation lines. The tool
is deterministic — it always preserves the existing top entry's
continuation lines.

The tool contract:

- Non-existent CHANGELOG.md → creates with header + entry.
- Empty section (no bullets) → appends entry after header.
- Single-line top entry → inserts new entry above it.
- Multi-line top entry → inserts before the complete block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_mill.agents import changelog_tool
from robotsix_mill.agents.changelog_tool import _insert_changelog_entry, _HEADER


def test_creates_file_when_missing(tmp_path: Path):
    result = _insert_changelog_entry(tmp_path, "- **foo**: bar")
    assert "created CHANGELOG.md" in result
    content = (tmp_path / "CHANGELOG.md").read_text()
    assert content == f"{_HEADER}\n\n- **foo**: bar\n"


def test_appends_entry_when_section_empty(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{_HEADER}\n\n")
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "appended entry" in result
    content = changelog.read_text()
    assert content == f"{_HEADER}\n\n- **new**: entry\n"


def test_inserts_before_single_line_top_entry(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{_HEADER}\n\n- **old**: first\n")
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    assert content == f"{_HEADER}\n\n- **new**: entry\n- **old**: first\n"


def test_inserts_before_multi_line_top_entry(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        f"{_HEADER}\n\n"
        "- **old**: first line\n"
        "  continuation line 1\n"
        "  continuation line 2\n"
        "- **second**: bullet\n"
    )
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    expected = (
        f"{_HEADER}\n\n"
        "- **new**: entry\n"
        "- **old**: first line\n"
        "  continuation line 1\n"
        "  continuation line 2\n"
        "- **second**: bullet\n"
    )
    assert content == expected


def test_preserves_continuation_with_tab_indent(tmp_path: Path):
    """Tab-indented continuations (rare but valid markdown) are preserved."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        f"{_HEADER}\n\n"
        "- **old**: first line\n"
        "\tcontinued with tab\n"
        "- **second**: bullet\n"
    )
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    expected = (
        f"{_HEADER}\n\n"
        "- **new**: entry\n"
        "- **old**: first line\n"
        "\tcontinued with tab\n"
        "- **second**: bullet\n"
    )
    assert content == expected


def test_handles_entry_with_continuation_lines(tmp_path: Path):
    """The new entry itself can have continuation lines."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{_HEADER}\n\n- **old**: single\n")
    new_entry = "- **new**: multi-line\n  detail line 1\n  detail line 2"
    result = _insert_changelog_entry(tmp_path, new_entry)
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    expected = (
        f"{_HEADER}\n\n"
        "- **new**: multi-line\n"
        "  detail line 1\n"
        "  detail line 2\n"
        "- **old**: single\n"
    )
    assert content == expected


def test_rejects_entry_without_bullet_prefix(tmp_path: Path):
    result = _insert_changelog_entry(tmp_path, "plain text without bullet")
    assert "must start with '- '" in result


def test_adds_header_when_missing(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Old header\n\n- old entry\n")
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "added header + entry" in result
    content = changelog.read_text()
    assert content.startswith(f"{_HEADER}\n\n- **new**: entry\n")
    assert "old entry" in content


def test_preserves_all_prior_entries(tmp_path: Path):
    """Regression: after an insert, every pre-existing entry must survive."""
    prior_entries = [
        "- **first**: entry one\n",
        "- **second**: entry two\n",
        "- **third**: multi-line\n  continuation a\n  continuation b\n",
        "- **fourth**: entry four\n",
        "- **fifth**: entry five\n",
    ]
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{_HEADER}\n\n" + "".join(prior_entries))

    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "inserted entry before existing top entry" in result

    content = changelog.read_text()
    # New entry is present
    assert "- **new**: entry" in content
    # Every prior entry survives intact
    for entry_text in prior_entries:
        assert entry_text in content, f"Missing prior entry: {entry_text!r}"


# ===========================================================================
# add_changelog_fragment — the standard-conforming replacement
# ===========================================================================


class TestAddChangelogFragment:
    """One file per ticket, so parallel PRs never touch a shared region.

    ``insert_changelog_entry`` wrote every ticket's entry to the same spot in
    CHANGELOG.md, which made any two open PRs conflict pairwise.
    """

    def _pyproject(self, tmp_path, directory="changelog.d", types=("bugfix", "misc")):
        blocks = "\n".join(
            f'  [[tool.towncrier.type]]\n  directory = "{t}"\n' for t in types
        )
        (tmp_path / "pyproject.toml").write_text(
            f'[tool.towncrier]\ndirectory = "{directory}"\n\n{blocks}',
            encoding="utf-8",
        )

    def test_writes_a_file_named_for_the_ticket(self, tmp_path):
        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        self._pyproject(tmp_path)
        result = _add_changelog_fragment(
            tmp_path, "20260807T120000Z-x-1a2b", "bugfix", "Fixed a thing."
        )
        written = tmp_path / "changelog.d" / "20260807T120000Z-x-1a2b.bugfix.md"
        assert written.read_text(encoding="utf-8") == "Fixed a thing.\n"
        assert "20260807T120000Z-x-1a2b.bugfix.md" in result

    def test_two_tickets_never_collide(self, tmp_path):
        """The whole point: distinct tickets produce distinct files."""
        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        self._pyproject(tmp_path)
        _add_changelog_fragment(tmp_path, "ticket-a", "bugfix", "A.")
        _add_changelog_fragment(tmp_path, "ticket-b", "bugfix", "B.")
        names = sorted(p.name for p in (tmp_path / "changelog.d").iterdir())
        assert names == ["ticket-a.bugfix.md", "ticket-b.bugfix.md"]

    def test_honours_a_repo_specific_directory(self, tmp_path):
        """auto-mail uses `changelog/`, not `changelog.d/`."""
        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        self._pyproject(tmp_path, directory="changelog")
        _add_changelog_fragment(tmp_path, "t", "misc", "Entry.")
        assert (tmp_path / "changelog" / "t.misc.md").exists()

    def test_maps_a_kind_to_the_repo_own_spelling(self, tmp_path):
        """llmio names its types feat/fix; the rest of the fleet uses
        feature/bugfix. An agent picking the majority spelling must not be
        rejected — that blocked llmio ticket
        20260805T173849Z-expose-robotsix-llmio-version-via-a-file-a8d7."""
        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        self._pyproject(tmp_path, types=("feat", "fix"))  # llmio's naming
        _add_changelog_fragment(tmp_path, "t", "bugfix", "Entry.")
        assert (tmp_path / "changelog.d" / "t.fix.md").exists()

    def test_rejects_a_kind_with_no_configured_equivalent(self, tmp_path):
        """A wrong extension is silently skipped by towncrier build, so the
        entry would vanish with no error — fail loudly instead."""
        import pytest

        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        self._pyproject(tmp_path, types=("feat", "fix"))
        with pytest.raises(ValueError, match="no configured equivalent"):
            _add_changelog_fragment(tmp_path, "t", "nonsense", "Entry.")

    def test_rejects_an_empty_summary(self, tmp_path):
        import pytest

        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        self._pyproject(tmp_path)
        with pytest.raises(ValueError, match="must not be empty"):
            _add_changelog_fragment(tmp_path, "t", "misc", "   ")

    def test_falls_back_when_pyproject_is_missing(self, tmp_path):
        """Best-effort beats recording nothing."""
        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        _add_changelog_fragment(tmp_path, "t", "misc", "Entry.")
        assert (tmp_path / "changelog.d" / "t.misc.md").exists()

    def test_does_not_touch_changelog_md(self, tmp_path):
        """The regression guard: CHANGELOG.md must be left alone entirely."""
        from robotsix_mill.agents.changelog_tool import _add_changelog_fragment

        self._pyproject(tmp_path)
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("## 0.0.0 (unreleased)\n\n- existing\n", encoding="utf-8")
        before = changelog.read_text(encoding="utf-8")
        _add_changelog_fragment(tmp_path, "t", "misc", "Entry.")
        assert changelog.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Kind resolution across the fleet's inconsistent towncrier type names
# ---------------------------------------------------------------------------


def _repo_with_types(tmp_path: Path, types: list[str], directory: str = "changelog.d"):
    """Write a minimal pyproject declaring *types* as towncrier kinds."""
    blocks = "\n".join(
        f'[[tool.towncrier.type]]\ndirectory = "{t}"\nname = "{t}"\nshowcontent = true\n'
        for t in types
    )
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.towncrier]\ndirectory = "{directory}"\n\n{blocks}', encoding="utf-8"
    )
    return tmp_path


class TestKindResolution:
    """robotsix-llmio names its types feat/fix; every other repo uses
    feature/bugfix. An agent that picks the majority spelling was getting a
    hard error, which blocked the ticket — llmio
    20260805T173849Z-expose-robotsix-llmio-version-via-a-file-a8d7 blocked
    exactly this way."""

    def test_exact_match_is_used_unchanged(self, tmp_path: Path) -> None:
        repo = _repo_with_types(tmp_path, ["feature", "bugfix", "misc"])
        out = changelog_tool._add_changelog_fragment(repo, "t-1", "feature", "x")
        assert out.endswith(".feature.md")

    def test_feature_falls_back_to_feat(self, tmp_path: Path) -> None:
        """The llmio case."""
        repo = _repo_with_types(tmp_path, ["feat", "fix", "doc", "removal", "misc"])
        out = changelog_tool._add_changelog_fragment(repo, "t-2", "feature", "x")
        assert out.endswith(".feat.md")
        assert (repo / "changelog.d" / "t-2.feat.md").read_text().strip() == "x"

    def test_bugfix_falls_back_to_fix(self, tmp_path: Path) -> None:
        repo = _repo_with_types(tmp_path, ["feat", "fix", "misc"])
        out = changelog_tool._add_changelog_fragment(repo, "t-3", "bugfix", "x")
        assert out.endswith(".fix.md")

    def test_fix_falls_back_to_bugfix(self, tmp_path: Path) -> None:
        """The reverse direction matters too — most repos use bugfix."""
        repo = _repo_with_types(tmp_path, ["feature", "bugfix", "misc"])
        out = changelog_tool._add_changelog_fragment(repo, "t-4", "fix", "x")
        assert out.endswith(".bugfix.md")

    def test_unmappable_kind_still_raises(self, tmp_path: Path) -> None:
        """Silently writing an unconfigured extension would lose the entry —
        towncrier build skips it without complaint."""
        repo = _repo_with_types(tmp_path, ["feature", "bugfix"])
        with pytest.raises(ValueError, match="no configured equivalent"):
            changelog_tool._add_changelog_fragment(repo, "t-5", "nonsense", "x")

    def test_error_lists_the_repo_actual_kinds(self, tmp_path: Path) -> None:
        repo = _repo_with_types(tmp_path, ["feat", "fix"])
        with pytest.raises(ValueError, match="feat, fix"):
            changelog_tool._add_changelog_fragment(repo, "t-6", "nonsense", "x")
