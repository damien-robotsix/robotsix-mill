"""Tests for changelog_autofill_runner — monkeypatch-only, no real I/O."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from robotsix_mill.runners.changelog_autofill_runner import (
    _format_entry,
    run_changelog_autofill_pass,
)


def _mk_pr(number, branch, title, author_login="dependabot[bot]"):
    return {
        "number": number,
        "branch": branch,
        "title": title,
        "author_login": author_login,
        "url": f"https://github.com/owner/repo/pull/{number}",
    }


# ---------------------------------------------------------------------------
# _format_entry
# ---------------------------------------------------------------------------


class TestFormatEntry:
    def test_basic_title(self):
        assert _format_entry("Bump foo from 1 to 2") == "- Bump foo from 1 to 2."

    def test_trailing_period_no_double(self):
        assert _format_entry("Bump foo from 1 to 2.") == "- Bump foo from 1 to 2."

    def test_trailing_whitespace_and_period(self):
        assert _format_entry("  Fix crash on startup.  ") == "- Fix crash on startup."

    def test_multiple_sentences(self):
        assert (
            _format_entry("Fix crash. Improve logging.")
            == "- Fix crash. Improve logging."
        )


# ---------------------------------------------------------------------------
# run_changelog_autofill_pass — integration via monkeypatch
# ---------------------------------------------------------------------------


class TestRunChangelogAutofillPass:
    def test_no_repo_config(self):
        """repo_config=None → returns immediately, get_forge never called."""
        with patch(
            "robotsix_mill.runners.changelog_autofill_runner.get_forge"
        ) as mock_gf:
            run_changelog_autofill_pass(session_id="s1", repo_config=None)
            mock_gf.assert_not_called()

    def test_skip_skip_changelog_label(self):
        """PR with Skip-Changelog label → no clone, no commit."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [_mk_pr(1, "br", "Title")]
        mock_forge.get_pr_labels.return_value = ["Skip-Changelog"]

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.git_ops"
            ) as mock_git,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_not_called()
        mock_git.commit_file.assert_not_called()

    def test_skip_check_passing(self):
        """check_status → success with no failing → skip."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [_mk_pr(1, "br", "Title")]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "success",
            "failing": [],
        }

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.git_ops"
            ) as mock_git,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_not_called()

    def test_skip_no_changelog_check_failing(self):
        """failing=[{"name":"lint"}] → 'changelog' not in names → skip."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [_mk_pr(1, "br", "Title")]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "lint"}],
        }

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.git_ops"
            ) as mock_git,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_not_called()

    def test_skip_changelog_in_pr_files(self):
        """changelog check failing but CHANGELOG.md already in diff → skip."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [_mk_pr(1, "br", "Title")]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "changelog"}],
        }
        mock_forge.pr_files.return_value = [
            {"path": "src/foo.py"},
            {"path": "CHANGELOG.md"},
        ]

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.git_ops"
            ) as mock_git,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_not_called()

    def test_commits_entry_for_bot_pr(self):
        """Bot PR with failing changelog, no CHANGELOG.md in diff → entry committed."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [
            _mk_pr(42, "dependabot/npm/leftpad-2.0.0", "Bump leftpad from 1 to 2")
        ]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "changelog"}],
        }
        mock_forge.pr_files.return_value = [{"path": "package.json"}]

        mock_git = MagicMock()
        mock_git.commit_file.return_value = True

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch("robotsix_mill.runners.changelog_autofill_runner.git_ops", mock_git),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner._insert_changelog_entry"
            ) as mock_insert,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_called_once()
        mock_insert.assert_called_once()
        mock_git.commit_file.assert_called_once()
        mock_git.push_with_lease.assert_called_once()

    def test_commits_entry_for_human_pr(self):
        """Human-authored PR with failing changelog → also gets an entry."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [
            _mk_pr(7, "alice/fix-typo", "Fix typo in README", author_login="alice")
        ]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "Changelog"}],  # case-insensitive match
        }
        mock_forge.pr_files.return_value = [{"path": "README.md"}]

        mock_git = MagicMock()
        mock_git.commit_file.return_value = True

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch("robotsix_mill.runners.changelog_autofill_runner.git_ops", mock_git),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner._insert_changelog_entry"
            ) as mock_insert,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_called_once()
        mock_insert.assert_called_once()
        mock_git.commit_file.assert_called_once()
        mock_git.push_with_lease.assert_called_once()

    def test_commit_file_false_skips_push(self):
        """commit_file returns False → no push called."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [_mk_pr(1, "br", "Title")]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "changelog"}],
        }
        mock_forge.pr_files.return_value = [{"path": "src/x.py"}]

        mock_git = MagicMock()
        mock_git.commit_file.return_value = False

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch("robotsix_mill.runners.changelog_autofill_runner.git_ops", mock_git),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner._insert_changelog_entry"
            ),
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_called_once()
        mock_git.commit_file.assert_called_once()
        mock_git.push_with_lease.assert_not_called()

    def test_push_error_continues_next_pr(self):
        """First PR raises on push; second PR is still processed."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [
            _mk_pr(1, "br1", "Title 1"),
            _mk_pr(2, "br2", "Title 2"),
        ]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "changelog"}],
        }
        mock_forge.pr_files.return_value = [{"path": "src/x.py"}]

        mock_git = MagicMock()
        mock_git.commit_file.return_value = True
        # First push raises, second succeeds.
        mock_git.push_with_lease.side_effect = [
            RuntimeError("remote rejected"),
            None,
        ]

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch("robotsix_mill.runners.changelog_autofill_runner.git_ops", mock_git),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner._insert_changelog_entry"
            ),
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        # Both PRs should have had clone + commit called.
        assert mock_git.clone.call_count == 2
        assert mock_git.commit_file.call_count == 2
        # push_with_lease called for both (first raised, second succeeded).
        assert mock_git.push_with_lease.call_count == 2

    def test_entry_format_passed_to_insert(self):
        """Title is normalized before passing to _insert_changelog_entry."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [
            _mk_pr(1, "br", "Bump foo from 1 to 2")
        ]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "changelog"}],
        }
        mock_forge.pr_files.return_value = [{"path": "src/x.py"}]

        mock_git = MagicMock()
        mock_git.commit_file.return_value = True

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch("robotsix_mill.runners.changelog_autofill_runner.git_ops", mock_git),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner._insert_changelog_entry"
            ) as mock_insert,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_insert.assert_called_once()
        # The second positional arg is the entry text
        _repo_dir, entry = mock_insert.call_args[0]
        assert entry == "- Bump foo from 1 to 2."

    def test_entry_format_trailing_period(self):
        """Title already ends with period → no double period."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [
            _mk_pr(1, "br", "Bump foo from 1 to 2.")
        ]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "changelog"}],
        }
        mock_forge.pr_files.return_value = [{"path": "src/x.py"}]

        mock_git = MagicMock()
        mock_git.commit_file.return_value = True

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch("robotsix_mill.runners.changelog_autofill_runner.git_ops", mock_git),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner._insert_changelog_entry"
            ) as mock_insert,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        _repo_dir, entry = mock_insert.call_args[0]
        assert entry == "- Bump foo from 1 to 2."

    def test_status_is_none_skips(self):
        """check_status returns None → skip."""
        mock_forge = MagicMock()
        mock_forge.list_open_prs.return_value = [_mk_pr(1, "br", "Title")]
        mock_forge.get_pr_labels.return_value = []
        mock_forge.check_status.return_value = None

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                return_value="tok",
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.git_ops"
            ) as mock_git,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        mock_git.clone.assert_not_called()

    def test_token_error_returns_early(self):
        """github_token raises RuntimeError → function returns without iterating PRs."""
        mock_forge = MagicMock()

        with (
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.get_forge",
                return_value=mock_forge,
            ),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.github_token",
                side_effect=RuntimeError("no token"),
            ),
            patch("robotsix_mill.runners.changelog_autofill_runner.Settings"),
            patch(
                "robotsix_mill.runners.changelog_autofill_runner.git_ops"
            ) as mock_git,
        ):
            run_changelog_autofill_pass(
                session_id="s1",
                repo_config=MagicMock(forge_remote_url="https://example.com/repo"),
            )

        # list_open_prs is called before github_token, but no PRs should be
        # processed because we return early on token error.
        mock_forge.check_status.assert_not_called()
        mock_git.clone.assert_not_called()
