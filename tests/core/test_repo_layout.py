"""Unit tests for ``robotsix_mill.core.repo_layout``."""

from robotsix_mill.core.repo_layout import resolve_under_src, src_path_candidates


# ---------------------------------------------------------------------------
# src_path_candidates
# ---------------------------------------------------------------------------


class TestSrcPathCandidates:
    """Tests for ``src_path_candidates`` — the pure candidate-generator."""

    def test_plain_relative_returns_token_then_src_prefixed(self):
        """A plain relative token returns [token, src/token]."""
        result = src_path_candidates("robotsix_llmio/core")
        assert result == ["robotsix_llmio/core", "src/robotsix_llmio/core"]

    def test_already_src_prefixed_returns_single(self):
        """A token already under src/ is not double-prefixed."""
        result = src_path_candidates("src/robotsix_mill/core/notify.py")
        assert result == ["src/robotsix_mill/core/notify.py"]

    def test_already_src_prefixed_case_insensitive(self):
        """Case-insensitive check on src/ prefix."""
        result = src_path_candidates("SRC/robotsix_mill/core/notify.py")
        assert result == ["SRC/robotsix_mill/core/notify.py"]

    def test_absolute_path_unchanged(self):
        """An absolute path is returned as-is (single candidate)."""
        result = src_path_candidates("/etc/passwd")
        assert result == ["/etc/passwd"]

    def test_leading_dot_slash_normalised(self):
        """A leading ./ is stripped before candidate generation."""
        result = src_path_candidates("./robotsix_llmio/core")
        assert result == ["robotsix_llmio/core", "src/robotsix_llmio/core"]

    def test_empty_token(self):
        """Empty string returns [token, src/token]."""
        result = src_path_candidates("")
        assert result == ["", "src/"]

    def test_just_src_prefix(self):
        """Token that IS just 'src/' returns as single candidate."""
        result = src_path_candidates("src/")
        assert result == ["src/"]

    def test_order_preserved(self):
        """Literal token always comes first."""
        tokens = [
            "core",
            "src/robotsix_mill/agents",
            "./config",
            "/absolute/path",
            "SRC/FOO/BAR",
            "robotsix_llmio/core/sqlite_util.py",
        ]
        for token in tokens:
            candidates = src_path_candidates(token)
            # First candidate is always the normalised literal.
            assert candidates[0] == token.lstrip("./") or candidates[0] == token


# ---------------------------------------------------------------------------
# resolve_under_src
# ---------------------------------------------------------------------------


class TestResolveUnderSrc:
    """Tests for ``resolve_under_src`` — the filesystem resolver."""

    def test_token_only_under_src_resolves(self, tmp_path):
        """A token that exists only at src/<token> returns that path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src" / "robotsix_llmio" / "core").mkdir(parents=True)
        (repo / "src" / "robotsix_llmio" / "core" / "models.py").write_text("")

        result = resolve_under_src(repo, "robotsix_llmio/core/models.py")
        assert result == repo / "src" / "robotsix_llmio" / "core" / "models.py"

    def test_token_at_repo_root_preferred(self, tmp_path):
        """A token that exists at repo root returns the root path
        (literal candidate is tried first and wins)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("hello")
        # Also create a src/ copy so we can assert literal is preferred.
        (repo / "src" / "README.md").parent.mkdir(parents=True, exist_ok=True)
        (repo / "src" / "README.md").write_text("src copy")

        result = resolve_under_src(repo, "README.md")
        assert result == repo / "README.md"

    def test_token_exists_neither_returns_none(self, tmp_path):
        """A token that exists neither at root nor under src/ → None."""
        repo = tmp_path / "repo"
        repo.mkdir()

        result = resolve_under_src(repo, "does_not_exist.txt")
        assert result is None

    def test_token_already_src_prefixed_not_double_prefixed(self, tmp_path):
        """src/<token> that exists → returned.  No src/src/<token> probe."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src" / "robotsix_mill" / "core").mkdir(parents=True)
        (repo / "src" / "robotsix_mill" / "core" / "notify.py").write_text("")

        result = resolve_under_src(repo, "src/robotsix_mill/core/notify.py")
        assert result == repo / "src" / "robotsix_mill" / "core" / "notify.py"

    def test_absolute_token_returns_none(self, tmp_path):
        """An absolute token like /etc/passwd cannot exist under repo_dir
        (the path join would be bizarre), but the function must not
        raise — return None."""
        repo = tmp_path / "repo"
        repo.mkdir()

        result = resolve_under_src(repo, "/etc/passwd")
        # /etc/passwd won't exist under repo; returns None.
        assert result is None

    def test_directory_resolves(self, tmp_path):
        """A directory that exists only under src/ resolves correctly."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src" / "robotsix_llmio" / "config").mkdir(parents=True)

        result = resolve_under_src(repo, "robotsix_llmio/config")
        assert result == repo / "src" / "robotsix_llmio" / "config"

    def test_defensive_no_raise_on_bytes_in_token(self, tmp_path):
        """A pathological token must not raise — returns None."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # This is a very defensive test — a bytes token shouldn't
        # normally reach this function, but if it does, it must not
        # crash the caller.
        result = resolve_under_src(repo, 42)  # type: ignore[arg-type]
        assert result is None

    def test_src_only_exists_at_root_returns_root(self, tmp_path):
        """When both root and src/ candidates exist, root wins
        (literal-first ordering)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config").mkdir()
        (repo / "src" / "config").mkdir(parents=True)

        result = resolve_under_src(repo, "config")
        assert result == repo / "config"

    def test_nonexistent_repo_dir_returns_none(self, tmp_path):
        """A repo_dir that doesn't exist at all → returns None
        (the .exists() check on candidates will be False)."""
        repo = tmp_path / "nonexistent"

        result = resolve_under_src(repo, "anything")
        assert result is None

    def test_leading_dot_slash_normalised(self, tmp_path):
        """./<token> is normalised to <token> and resolves."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src" / "robotsix_llmio" / "core").mkdir(parents=True)
        (repo / "src" / "robotsix_llmio" / "core" / "util.py").write_text("")

        result = resolve_under_src(repo, "./robotsix_llmio/core/util.py")
        assert result == repo / "src" / "robotsix_llmio" / "core" / "util.py"
