"""Tests for bridged git tool closures — guardrail rejections and token-never-leaked.

The guardrail tests intentionally do NOT create a real git repo — the
guardrail check is the first line of each closure, so no shell-out occurs
on the rejection path.  This keeps the tests fast and offline.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from unittest.mock import patch

from robotsix_mill.agents.bridged_git_tools import build_bridged_git_tools

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TOOLS_BRANCH = "mill/t-1"
_TOOLS_TARGET = "main"
_TOOLS_DIR = Path("/tmp/dummy_repo")
_TOOLS_URL = "https://github.com/o/r.git"


def _make_tools(**overrides):
    kwargs = {
        "repo_dir": _TOOLS_DIR,
        "branch": _TOOLS_BRANCH,
        "target": _TOOLS_TARGET,
        "remote_url": _TOOLS_URL,
        "token": None,
    }
    kwargs.update(overrides)
    return build_bridged_git_tools(**kwargs)


# ---------------------------------------------------------------------------
# 1a. Guardrail rejection tests
# ---------------------------------------------------------------------------


class TestGuardrailRejections:
    """Each tool rejects a mismatched branch/target argument with a
    deterministic error string (no shell-out, no mocks needed)."""

    def test_git_fetch_guardrail_rejects_other_branch(self):
        git_fetch, _, _, _ = _make_tools()
        result = git_fetch("other")
        assert result == (
            "error: git_fetch is guardrailed to target branch 'main' — 'other' rejected"
        )

    def test_git_remote_sha_guardrail_rejects_other_branch(self):
        _, git_remote_sha, _, _ = _make_tools()
        result = git_remote_sha("other")
        assert result == (
            "error: git_remote_sha is guardrailed to ticket branch "
            "'mill/t-1' — 'other' rejected"
        )

    def test_git_push_with_lease_guardrail_rejects_other_branch(self):
        _, _, git_push_with_lease, _ = _make_tools()
        result = git_push_with_lease("other")
        assert result == (
            "error: git_push_with_lease is guardrailed to ticket branch "
            "'mill/t-1' — 'other' rejected"
        )

    def test_git_branch_ancestry_guardrail_rejects_other_branch(self):
        _, _, _, git_branch_ancestry = _make_tools()
        result = git_branch_ancestry("other", "main")
        assert result == (
            "error: git_branch_ancestry is guardrailed to ticket branch "
            "'mill/t-1' — 'other' rejected"
        )

    def test_git_branch_ancestry_guardrail_rejects_other_target(self):
        _, _, _, git_branch_ancestry = _make_tools()
        result = git_branch_ancestry("mill/t-1", "other")
        assert result == (
            "error: git_branch_ancestry is guardrailed to target branch "
            "'main' — 'other' rejected"
        )


# ---------------------------------------------------------------------------
# 1b. Token-never-leaked
# ---------------------------------------------------------------------------


class TestTokenNeverLeaked:
    """The closures capture a ``token`` kwarg but must never return it
    — not in success output, not in error output."""

    TOKEN = "ghs_sekret999"
    # An exception that mimics a real git failure with the token in both
    # the URL arg and a raw string inside stderr.
    _EXC = subprocess.CalledProcessError(
        128,
        [
            "git",
            "fetch",
            f"https://oauth2:{TOKEN}@github.com/o/r.git",
            "+refs/heads/mill/t-1:refs/remotes/origin/mill/t-1",
        ],
        output="",
        stderr=f"fatal: token {TOKEN} in stderr\n",
    )

    def _build(self, tmp_path):
        return build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/t-1",
            target="main",
            remote_url="https://github.com/o/r.git",
            token=self.TOKEN,
        )

    # -- happy path -------------------------------------------------------

    def test_happy_path_no_token_in_output(self, tmp_path):
        git_fetch, git_remote_sha, git_push_with_lease, git_branch_ancestry = (
            self._build(tmp_path)
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch") as mock_fetch,
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                return_value="abc1234",
            ),
            # The tool re-reads the remote after pushing and compares it to
            # the local HEAD — both must agree for PUSH_OK.
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.head_sha",
                return_value="abc1234",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.branch_ancestry",
                return_value=[],
            ),
        ):
            r1 = git_fetch("main")
            assert "ghs_" not in r1
            assert r1 == "fetched origin/main"

            r2 = git_remote_sha("mill/t-1")
            assert "ghs_" not in r2
            assert r2 == "abc1234"

            r3 = git_push_with_lease("mill/t-1")
            assert "ghs_" not in r3
            assert r3 == "PUSH_OK"

            r4 = git_branch_ancestry("mill/t-1", "main")
            assert "ghs_" not in r4
            assert r4 == "(no commits ahead of target — branches are identical)"

            # fetch was called by all four tools: git_fetch x1, git_remote_sha x1,
            # git_push_with_lease x2 (pre-push + post-push verify), git_branch_ancestry x2
            # = 6 total.
            assert mock_fetch.call_count == 6

    # -- error: git_fetch & git_remote_sha ---------------------------------

    def test_git_fetch_error_redacted(self, tmp_path):
        git_fetch, _, _, _ = self._build(tmp_path)

        with patch(
            "robotsix_mill.agents.bridged_git_tools.git_ops.fetch",
            side_effect=self._EXC,
        ):
            result = git_fetch("main")

        assert self.TOKEN not in result
        assert "ghs_" not in result
        assert "***" in result
        assert result.startswith("error: git_fetch failed:")

    def test_git_remote_sha_error_redacted(self, tmp_path):
        _, git_remote_sha, _, _ = self._build(tmp_path)

        with patch(
            "robotsix_mill.agents.bridged_git_tools.git_ops.fetch",
            side_effect=self._EXC,
        ):
            result = git_remote_sha("mill/t-1")

        assert self.TOKEN not in result
        assert "ghs_" not in result
        assert "***" in result
        assert result.startswith("error: fetch before remote_sha failed:")

    # -- first-push (remote branch doesn't exist yet) -----------------------

    def test_git_push_with_lease_first_push(self, tmp_path):
        """When the remote branch doesn't exist, the fetch raises
        'couldn't find remote ref'.  The tool should tolerate that and
        proceed to push_with_lease (which first-pushes via --force)."""
        _, _, git_push_with_lease, _ = self._build(tmp_path)

        missing_ref_exc = subprocess.CalledProcessError(
            128,
            ["git", "fetch", "..."],
            output="",
            stderr="fatal: couldn't find remote ref refs/heads/mill/t-1\n",
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch") as mock_fetch,
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                return_value="abc1234",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.head_sha",
                return_value="abc1234",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
            ) as mock_push,
        ):
            # First fetch (missing remote ref) raises; the post-push verify
            # fetch must succeed so the tool can confirm the push landed.
            mock_fetch.side_effect = [missing_ref_exc, None]
            result = git_push_with_lease("mill/t-1")

        assert result == "PUSH_OK"
        # Pre-push fetch + post-push verify fetch.
        assert mock_fetch.call_count == 2
        mock_push.assert_called_once()

    # -- error: git_push_with_lease ----------------------------------------

    def test_git_push_with_lease_error_redacted(self, tmp_path):
        _, _, git_push_with_lease, _ = self._build(tmp_path)

        push_exc = subprocess.CalledProcessError(
            128,
            [
                "git",
                "push",
                f"https://oauth2:{self.TOKEN}@github.com/o/r.git",
                "mill/t-1:mill/t-1",
            ],
            output="",
            stderr=f"fatal: token {self.TOKEN} in push stderr\n",
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch") as mock_fetch,
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
                side_effect=push_exc,
            ),
        ):
            result = git_push_with_lease("mill/t-1")

        assert self.TOKEN not in result
        assert "ghs_" not in result
        assert "***" in result
        # The stderr doesn't contain "stale" or "[rejected]", so it's PUSH_ERROR.
        assert result.startswith("PUSH_ERROR:")
        # fetch was called once before the push.
        mock_fetch.assert_called_once()

    def test_git_push_with_lease_auth_error(self, tmp_path):
        """When the push fails with an auth message, the tool returns
        PUSH_AUTH_ERROR (not generic PUSH_ERROR), so the ci_fix agent
        can emit a classified diagnostic."""
        _, _, git_push_with_lease, _ = self._build(tmp_path)

        auth_exc = subprocess.CalledProcessError(
            128,
            [
                "git",
                "push",
                f"https://oauth2:{self.TOKEN}@github.com/o/r.git",
                "mill/t-1:mill/t-1",
            ],
            output="",
            stderr=(
                b"remote: Invalid username or token.\n"
                b"fatal: Authentication failed for "
                b"'https://github.com/o/r.git/'\n"
            ),
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch") as mock_fetch,
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
                side_effect=auth_exc,
            ),
        ):
            result = git_push_with_lease("mill/t-1")

        assert self.TOKEN not in result
        assert "ghs_" not in result
        # The auth error is classified distinctly from generic PUSH_ERROR.
        assert result.startswith("PUSH_AUTH_ERROR:")
        # The stderr is redacted (the error message itself doesn't contain
        # a token, but redact_credentials is still called and is a no-op
        # on token-free text).
        assert "Invalid username or token" in result
        mock_fetch.assert_called_once()

    # -- push that does not move remote HEAD -------------------------------

    def test_git_push_with_lease_not_landed_fails_loudly(self, tmp_path):
        """A push that exits 0 but does NOT advance the remote branch (the
        post-push re-read shows remote HEAD != local HEAD) must never be
        reported as PUSH_OK — the tool retries once, then fails loudly, so
        the agent cannot report DONE on a lost push."""
        _, _, git_push_with_lease, _ = self._build(tmp_path)

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch") as mock_fetch,
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                return_value="remote-stale-sha",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.head_sha",
                return_value="local-fix-sha",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
            ) as mock_push,
        ):
            result = git_push_with_lease("mill/t-1")

        assert result.startswith("PUSH_ERROR:")
        assert "did not land" in result
        # Original push + one retry — the remote never advances, so the tool
        # gives up loudly instead of handing the agent a false PUSH_OK.
        assert mock_push.call_count == 2
        # Pre-push fetch + two post-push verify fetches.
        assert mock_fetch.call_count == 3

    def test_git_push_with_lease_not_landed_retry_recovers(self, tmp_path):
        """When the first verification shows the remote did not advance but
        the retry's verification does, the tool reports PUSH_OK (the push
        landed on the second attempt)."""
        _, _, git_push_with_lease, _ = self._build(tmp_path)

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                side_effect=["remote-stale-sha", "local-fix-sha"],
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.head_sha",
                return_value="local-fix-sha",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
            ) as mock_push,
        ):
            result = git_push_with_lease("mill/t-1")

        assert result == "PUSH_OK"
        assert mock_push.call_count == 2

    # -- error: git_branch_ancestry ----------------------------------------

    def test_git_branch_ancestry_error_redacted(self, tmp_path):
        _, _, _, git_branch_ancestry = self._build(tmp_path)

        with patch(
            "robotsix_mill.agents.bridged_git_tools.git_ops.fetch",
            side_effect=self._EXC,
        ):
            result = git_branch_ancestry("mill/t-1", "main")

        assert self.TOKEN not in result
        assert "ghs_" not in result
        assert "***" in result
        assert result.startswith("error: fetch before ancestry failed:")


# ---------------------------------------------------------------------------
# Token-refresh-on-auth-failure tests
# ---------------------------------------------------------------------------


class TestTokenRefreshOnAuthFailure:
    """When a push fails with an auth error, the tool should clear the
    token cache, obtain a fresh token, and retry once."""

    STALE_TOKEN = "ghs_stale_expired"
    FRESH_TOKEN = "ghs_fresh_new_token"

    def test_push_auth_retry_succeeds_with_fresh_token(self, tmp_path):
        """Auth failure on first push → cache clear → fresh token → retry succeeds."""
        call_count = 0
        tokens_used: list[str] = []

        def token_provider() -> str | None:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return self.STALE_TOKEN
            return self.FRESH_TOKEN

        cache_cleared = False

        def cache_clear() -> None:
            nonlocal cache_cleared
            cache_cleared = True

        auth_exc = subprocess.CalledProcessError(
            128,
            ["git", "push", "..."],
            output="",
            stderr=b"remote: Invalid username or token.\n",
        )

        def tracking_push(repo, branch, remote_url, token, **kw):
            tokens_used.append(token)
            if token == self.STALE_TOKEN:
                raise auth_exc

        _, _, git_push_with_lease, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/t-1",
            target="main",
            remote_url="https://github.com/o/r.git",
            token_provider=token_provider,
            token_cache_clear=cache_clear,
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                return_value="abc1234",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.head_sha",
                return_value="abc1234",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
                side_effect=tracking_push,
            ),
        ):
            result = git_push_with_lease("mill/t-1")

        assert result == "PUSH_OK"
        assert cache_cleared
        assert tokens_used == [self.STALE_TOKEN, self.FRESH_TOKEN]

    def test_push_auth_retry_still_fails_returns_auth_error(self, tmp_path):
        """Auth failure on first push → cache clear → fresh token → still fails → PUSH_AUTH_ERROR."""
        call_count = 0

        def token_provider() -> str | None:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return self.STALE_TOKEN
            return self.FRESH_TOKEN

        def cache_clear() -> None:
            pass

        auth_exc = subprocess.CalledProcessError(
            128,
            ["git", "push", "..."],
            output="",
            stderr=b"remote: Invalid username or token.\n",
        )

        _, _, git_push_with_lease, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/t-1",
            target="main",
            remote_url="https://github.com/o/r.git",
            token_provider=token_provider,
            token_cache_clear=cache_clear,
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
                side_effect=auth_exc,
            ),
        ):
            result = git_push_with_lease("mill/t-1")

        assert result.startswith("PUSH_AUTH_ERROR:")
        assert "Invalid username or token" in result

    def test_push_auth_no_retry_when_same_token(self, tmp_path):
        """When the provider returns the same token after cache clear,
        no retry is attempted (the token won't change)."""

        def token_provider() -> str | None:
            return self.STALE_TOKEN

        def cache_clear() -> None:
            pass

        auth_exc = subprocess.CalledProcessError(
            128,
            ["git", "push", "..."],
            output="",
            stderr=b"remote: Invalid username or token.\n",
        )

        push_calls: list[str] = []

        def tracking_push(repo, branch, remote_url, token, **kw):
            push_calls.append(token)
            raise auth_exc

        _, _, git_push_with_lease, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/t-1",
            target="main",
            remote_url="https://github.com/o/r.git",
            token_provider=token_provider,
            token_cache_clear=cache_clear,
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
                side_effect=tracking_push,
            ),
        ):
            result = git_push_with_lease("mill/t-1")

        assert result.startswith("PUSH_AUTH_ERROR:")
        # Only one push attempt — no retry since token didn't change.
        assert len(push_calls) == 1

    def test_push_auth_no_cache_clear_callback(self, tmp_path):
        """When token_cache_clear is not provided, auth failure still
        returns PUSH_AUTH_ERROR (graceful degradation)."""

        def token_provider() -> str | None:
            return self.STALE_TOKEN

        auth_exc = subprocess.CalledProcessError(
            128,
            ["git", "push", "..."],
            output="",
            stderr=b"remote: Invalid username or token.\n",
        )

        _, _, git_push_with_lease, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/t-1",
            target="main",
            remote_url="https://github.com/o/r.git",
            token_provider=token_provider,
            # No token_cache_clear
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease",
                side_effect=auth_exc,
            ),
        ):
            result = git_push_with_lease("mill/t-1")

        assert result.startswith("PUSH_AUTH_ERROR:")

    def test_backward_compat_static_token(self, tmp_path):
        """Static token kwarg still works (backward compatibility)."""
        _, _, git_push_with_lease, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/t-1",
            target="main",
            remote_url="https://github.com/o/r.git",
            token="ghs_static_token",
        )

        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                return_value="abc1234",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.head_sha",
                return_value="abc1234",
            ),
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease"),
        ):
            result = git_push_with_lease("mill/t-1")

        assert result == "PUSH_OK"


# --- trace_stage child-span tests ----------------------------------------


class TestTraceStageSpans:
    """Each bridged git tool opens a child span via trace_stage with the
    tool's registered name."""

    def test_git_fetch_emits_span(self, tmp_path, monkeypatch):
        import robotsix_mill.agents.bridged_git_tools as bgt

        spans: list[str] = []

        @contextlib.contextmanager
        def fake_trace_stage(name):
            spans.append(name)
            yield

        monkeypatch.setattr(bgt, "trace_stage", fake_trace_stage)
        git_fetch, _, _, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/x",
            target="main",
            remote_url="https://github.com/o/r.git",
            token=None,
        )
        with patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"):
            result = git_fetch("main")
        assert result == "fetched origin/main"
        assert spans == ["git_fetch"]

    def test_git_remote_sha_emits_span(self, tmp_path, monkeypatch):
        import robotsix_mill.agents.bridged_git_tools as bgt

        spans: list[str] = []

        @contextlib.contextmanager
        def fake_trace_stage(name):
            spans.append(name)
            yield

        monkeypatch.setattr(bgt, "trace_stage", fake_trace_stage)
        _, git_remote_sha, _, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/x",
            target="main",
            remote_url="https://github.com/o/r.git",
            token=None,
        )
        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                return_value="abc1234",
            ),
        ):
            result = git_remote_sha("mill/x")
        assert result == "abc1234"
        assert spans == ["git_remote_sha"]

    def test_git_push_with_lease_emits_span(self, tmp_path, monkeypatch):
        import robotsix_mill.agents.bridged_git_tools as bgt

        spans: list[str] = []

        @contextlib.contextmanager
        def fake_trace_stage(name):
            spans.append(name)
            yield

        monkeypatch.setattr(bgt, "trace_stage", fake_trace_stage)
        _, _, git_push_with_lease, _ = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/x",
            target="main",
            remote_url="https://github.com/o/r.git",
            token=None,
        )
        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.remote_branch_sha",
                return_value="abc1234",
            ),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.head_sha",
                return_value="abc1234",
            ),
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.push_with_lease"),
        ):
            result = git_push_with_lease("mill/x")
        assert result == "PUSH_OK"
        assert spans == ["git_push_with_lease"]

    def test_git_branch_ancestry_emits_span(self, tmp_path, monkeypatch):
        import robotsix_mill.agents.bridged_git_tools as bgt

        spans: list[str] = []

        @contextlib.contextmanager
        def fake_trace_stage(name):
            spans.append(name)
            yield

        monkeypatch.setattr(bgt, "trace_stage", fake_trace_stage)
        _, _, _, git_branch_ancestry = build_bridged_git_tools(
            repo_dir=tmp_path,
            branch="mill/x",
            target="main",
            remote_url="https://github.com/o/r.git",
            token=None,
        )
        with (
            patch("robotsix_mill.agents.bridged_git_tools.git_ops.fetch"),
            patch(
                "robotsix_mill.agents.bridged_git_tools.git_ops.branch_ancestry",
                return_value=[],
            ),
        ):
            result = git_branch_ancestry("mill/x", "main")
        assert result == "(no commits ahead of target — branches are identical)"
        assert spans == ["git_branch_ancestry"]
