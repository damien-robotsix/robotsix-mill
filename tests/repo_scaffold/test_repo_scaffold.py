"""Tests for ``robotsix_mill.repo_scaffold``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from robotsix_mill.config import (
    RepoConfig,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge.base import NotConfiguredError, RepoInfo
from robotsix_mill.repo_scaffold import (
    _append_repo_config,
    _python_package_name,
    _sanitize_repo_id,
    _write_github_workflows,
    _write_periodic_presence_files,
    _write_python_skeleton,
    run_repo_scaffold,
)
from robotsix_mill.stages.base import StageContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_cfg(
    repo_id: str,
    *,
    board_id: str | None = None,
    forge_remote_url: str | None = None,
    langfuse_public_key: str = "pk-mill",
    langfuse_secret_key: str = "sk-mill",
    langfuse_base_url: str = "https://cloud.langfuse.com",
) -> RepoConfig:
    return RepoConfig(
        repo_id=repo_id,
        board_id=board_id or repo_id,
        langfuse_project_name=f"proj-{repo_id}",
        langfuse_public_key=langfuse_public_key,
        langfuse_secret_key=langfuse_secret_key,
        langfuse_base_url=langfuse_base_url,
        forge_remote_url=forge_remote_url,
    )


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointed at tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Settings(**overrides)


def _make_params(**fields) -> dict:
    """Build a params dict for run_repo_scaffold (the replacement for
    the old marker-based approach)."""
    defaults = {
        "name": "my-new-repo",
        "owner": "my-org",
        "private": False,
        "description": "A test repo",
        "language": "python",
    }
    defaults.update(fields)
    return defaults


def _make_ticket(
    svc: TicketService,
    *,
    title: str = "Test new repo",
    description: str = "",
    source: str = SourceKind.META,
    state: State = State.READY,
) -> Ticket:
    """Create a ticket and optionally set its state."""
    ticket = svc.create(
        title=title,
        description=description,
        source=source,
    )
    if state != State.DRAFT:
        svc.transition(ticket.id, state)
    return svc.get(ticket.id)


def _stage_context(
    settings: Settings, ticket: Ticket, board_id: str = "meta"
) -> StageContext:
    svc = TicketService(settings, board_id=board_id)
    return StageContext(settings=settings, service=svc)


# ---------------------------------------------------------------------------
# _sanitize_repo_id
# ---------------------------------------------------------------------------


class TestSanitizeRepoId:
    def test_lowercase_and_hyphens(self):
        assert _sanitize_repo_id("My New Repo") == "my-new-repo"

    def test_underscores_to_hyphens(self):
        assert _sanitize_repo_id("my_repo_name") == "my-repo-name"

    def test_dots_to_hyphens(self):
        assert _sanitize_repo_id("my.repo") == "my-repo"

    def test_strips_leading_trailing_hyphens(self):
        assert _sanitize_repo_id("--hello--") == "hello"

    def test_non_ascii_to_hyphens(self):
        # Non-ASCII chars become hyphens; trailing hyphens are stripped
        assert _sanitize_repo_id("café") == "caf"
        assert _sanitize_repo_id("über") == "ber"


# ---------------------------------------------------------------------------
# _append_repo_config
# ---------------------------------------------------------------------------


class TestAppendRepoConfig:
    def test_structure(self, tmp_path, monkeypatch):
        """Verify the written YAML stanza has the exact fields."""
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))

        settings = _make_settings(
            tmp_path,
            trace_review_target_repo_id="robotsix-mill",
        )
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        repo_info = RepoInfo(
            id=42,
            name="my-new-repo",
            clone_url="https://github.com/my-org/my-new-repo.git",
            html_url="https://github.com/my-org/my-new-repo",
        )
        params = _make_params()

        _append_repo_config(repo_info, params, settings)

        # Read back and verify
        with open(repos_file, "r") as fh:
            data = yaml.safe_load(fh)

        repos = data["repos"]
        assert "my-new-repo" in repos
        entry = repos["my-new-repo"]

        assert entry["board_id"] == "my-new-repo"
        # Langfuse is configured globally (top-level ``langfuse`` block); the
        # new repo inherits it — no per-repo langfuse stanza is written.
        assert "langfuse" not in entry
        assert entry["forge_remote_url"] == "https://github.com/my-org/my-new-repo.git"
        # test_command + language are NOT written to repos.yaml — they live in
        # the new repo's own .robotsix-mill/config.yaml (the scaffold commit).
        assert "test_command" not in entry
        assert "language" not in entry
        # Periodic config is NOT written to repos.yaml (the loader ignores
        # it); enablement is per-repo file presence in the new repo.
        assert "periodic" not in entry

        _reset_repos_config()

    def test_new_repo_has_no_per_repo_langfuse_stanza(self, tmp_path, monkeypatch):
        """The scaffolded stanza carries NO langfuse block — observability is
        configured globally and the new repo inherits it."""
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))

        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        repo_info = RepoInfo(
            id=1,
            name="new-repo",
            clone_url="https://github.com/x/new-repo.git",
            html_url="https://github.com/x/new-repo",
        )
        params = _make_params(name="new-repo")

        _append_repo_config(repo_info, params, settings)

        with open(repos_file, "r") as fh:
            data = yaml.safe_load(fh)

        assert "langfuse" not in data["repos"]["new-repo"]

        _reset_repos_config()

    def test_appends_to_existing_file(self, tmp_path, monkeypatch):
        """Appending preserves existing entries in repos.yaml."""
        repos_file = tmp_path / "repos.yaml"
        existing = {
            "repos": {
                "existing-repo": {
                    "board_id": "existing-repo",
                    "langfuse": {
                        "project_name": "existing-repo",
                        "public_key": "pk-old",
                        "secret_key": "sk-old",
                    },
                }
            }
        }
        with open(repos_file, "w") as fh:
            yaml.dump(existing, fh)

        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))

        settings = _make_settings(
            tmp_path,
            trace_review_target_repo_id="robotsix-mill",
        )
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        repo_info = RepoInfo(
            id=2,
            name="second-repo",
            clone_url="https://github.com/x/second-repo.git",
            html_url="https://github.com/x/second-repo",
        )
        params = _make_params(name="second-repo")

        _append_repo_config(repo_info, params, settings)

        with open(repos_file, "r") as fh:
            data = yaml.safe_load(fh)

        assert "existing-repo" in data["repos"]
        assert "second-repo" in data["repos"]
        assert data["repos"]["existing-repo"]["board_id"] == "existing-repo"

        _reset_repos_config()

    def test_mill_repos_file_empty_string_skips_write(self, tmp_path, monkeypatch):
        """When MILL_REPOS_FILE='', no file is written (test-suite mode)."""
        monkeypatch.setenv("MILL_REPOS_FILE", "")

        settings = _make_settings(tmp_path)
        repo_info = RepoInfo(
            id=1,
            name="x",
            clone_url="https://example.com/x.git",
            html_url="https://example.com/x",
        )
        params = _make_params(name="x")

        # Should not raise
        _append_repo_config(repo_info, params, settings)


class TestAppendRepoConfigOverlay:
    def test_writes_to_data_dir_overlay(self, tmp_path, monkeypatch):
        """When MILL_REPOS_FILE is unset, _append_repo_config writes to
        <data_dir>/registered_repos.yaml."""
        monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        repo_info = RepoInfo(
            id=1,
            name="my-new-repo",
            clone_url="https://github.com/my-org/my-new-repo.git",
            html_url="https://github.com/my-org/my-new-repo",
        )
        params = _make_params()

        _append_repo_config(repo_info, params, settings)

        overlay = tmp_path / "data" / "registered_repos.yaml"
        assert overlay.exists()
        with open(overlay, "r") as fh:
            data = yaml.safe_load(fh)
        assert "my-new-repo" in data["repos"]
        assert data["repos"]["my-new-repo"]["board_id"] == "my-new-repo"

        _reset_repos_config()

    def test_data_dir_created_if_missing(self, tmp_path, monkeypatch):
        """When the data_dir subdirectory does not exist, _append_repo_config
        creates it before writing the overlay."""
        monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
        data_dir = tmp_path / "nested" / "data"
        settings = _make_settings(tmp_path, data_dir=str(data_dir))

        repo_info = RepoInfo(
            id=1,
            name="x",
            clone_url="https://example.com/x.git",
            html_url="https://example.com/x",
        )
        params = _make_params(name="x")

        assert not data_dir.exists()
        _append_repo_config(repo_info, params, settings)

        overlay = data_dir / "registered_repos.yaml"
        assert overlay.exists()

        _reset_repos_config()


# ---------------------------------------------------------------------------
# run_repo_scaffold
# ---------------------------------------------------------------------------


class TestRunRepoScaffold:
    def test_happy_path(self, tmp_path, monkeypatch):
        """Happy path: creates repo, scaffolds, appends config."""
        settings = _make_settings(
            tmp_path,
            trace_review_target_repo_id="robotsix-mill",
        )
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        # Register mill repo for langfuse key copy

        # Set up repos.yaml path
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))

        # Create a params dict directly (no marker parsing)
        params = _make_params(name="my-repo")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Create new-repo my-repo",
            description="Extract my-repo as a standalone library.",
        )
        ctx = _stage_context(settings, ticket)

        # Mock Forge
        forge = MagicMock()
        forge.create_repo.return_value = RepoInfo(
            id=99,
            name="my-repo",
            clone_url="https://github.com/my-org/my-repo.git",
            html_url="https://github.com/my-org/my-repo",
        )

        # Mock git_ops
        init_calls = []
        commit_calls = []
        push_calls = []

        def _fake_init_repo(dest, branch):
            init_calls.append({"dest": dest, "branch": branch})
            # Create the directory so subsequent writes work
            dest.mkdir(parents=True, exist_ok=True)

        def _fake_commit_all(repo, message):
            commit_calls.append({"repo": repo, "message": message})

        def _fake_push(repo, branch, remote_url, token):
            push_calls.append(
                {"repo": repo, "branch": branch, "remote_url": remote_url}
            )

        monkeypatch.setattr(
            "robotsix_mill.repo_scaffold.git_ops.init_repo", _fake_init_repo
        )
        monkeypatch.setattr(
            "robotsix_mill.repo_scaffold.git_ops.commit_all", _fake_commit_all
        )
        monkeypatch.setattr("robotsix_mill.repo_scaffold.git_ops.push", _fake_push)
        monkeypatch.setattr(
            "robotsix_mill.repo_scaffold.github_token",
            lambda s, repo_config=None: "fake-token",
        )
        # Prevent temp dir cleanup so we can inspect scaffolded files
        monkeypatch.setattr(
            "robotsix_mill.repo_scaffold.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )

        outcome = run_repo_scaffold(
            settings,
            forge,
            ctx,
            params,
            ticket_description="Extract my-repo as a standalone library.",
        )

        assert outcome.next_state == State.DONE
        forge.create_repo.assert_called_once_with(
            name="my-repo",
            owner="my-org",
            private=False,
            description="A test repo",
        )

        # git_ops.init_repo was called (empty remote — init, don't clone)
        assert len(init_calls) == 1
        assert init_calls[0]["branch"] == settings.forge_target_branch

        # git_ops.commit_all was called
        assert len(commit_calls) == 1
        assert commit_calls[0]["message"] == "Initial scaffold"

        # git_ops.push was called
        assert len(push_calls) == 1
        assert push_calls[0]["branch"] == settings.forge_target_branch
        assert push_calls[0]["remote_url"] == "https://github.com/my-org/my-repo.git"

        # Verify scaffold files were written in the init dest
        dest = init_calls[0]["dest"]
        assert (dest / "README.md").exists()
        assert (dest / "LICENSE").exists()
        assert (dest / "pyproject.toml").exists()
        # The package dir is import-safe (hyphens → underscores) so the
        # hatchling wheel build resolves — "my-repo" → "src/my_repo/".
        assert (dest / "src" / "my_repo" / "__init__.py").exists()
        assert not (dest / "src" / "my-repo").exists()
        pyproject = (dest / "pyproject.toml").read_text()
        assert 'name = "my-repo"' in pyproject  # distribution name keeps hyphen
        assert 'packages = ["src/my_repo"]' in pyproject  # explicit wheel target
        assert (dest / "tests" / "__init__.py").exists()
        # The scaffold writes the repo's own mill config (test_command +
        # languages) — NOT the operator's repos.yaml.
        repo_cfg = (dest / ".robotsix-mill" / "config.yaml").read_text()
        assert "test_command: pytest -q" in repo_cfg
        assert "languages: [python]" in repo_cfg

        # Verify repos.yaml was written
        assert repos_file.exists()
        with open(repos_file, "r") as fh:
            data = yaml.safe_load(fh)
        assert "my-repo" in data["repos"]

        _reset_repos_config()
        db.reset_engine()

    def test_not_configured_error_fallback(self, tmp_path, monkeypatch):
        """NotConfiguredError → BLOCKED."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Create new-repo",
            description="Create my-repo",
        )
        ctx = _stage_context(settings, ticket)

        forge = MagicMock()
        forge.create_repo.side_effect = NotConfiguredError("disabled")

        params = _make_params(name="my-repo")
        outcome = run_repo_scaffold(settings, forge, ctx, params)

        assert outcome.next_state == State.BLOCKED
        assert outcome.note == "Repo creation not configured"

        db.reset_engine()

    def test_repo_already_exists_fallback(self, tmp_path, monkeypatch):
        """RuntimeError('already exists') → BLOCKED."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Create new-repo",
            description="Create my-repo",
        )
        ctx = _stage_context(settings, ticket)

        forge = MagicMock()
        forge.create_repo.side_effect = RuntimeError("Repository already exists")

        params = _make_params(name="my-repo")
        outcome = run_repo_scaffold(settings, forge, ctx, params)

        assert outcome.next_state == State.BLOCKED
        assert outcome.note == "Repo 'my-repo' already exists"

        db.reset_engine()

    def test_other_runtime_error_propagates(self, tmp_path, monkeypatch):
        """Non-'already exists' RuntimeError propagates (not caught)."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Create new-repo",
            description="Create my-repo",
        )
        ctx = _stage_context(settings, ticket)

        forge = MagicMock()
        forge.create_repo.side_effect = RuntimeError("Some other error")

        params = _make_params(name="my-repo")
        with pytest.raises(RuntimeError, match="Some other error"):
            run_repo_scaffold(settings, forge, ctx, params)

        db.reset_engine()


# ---------------------------------------------------------------------------
# _write_python_skeleton
# ---------------------------------------------------------------------------


class TestWritePythonSkeleton:
    def test_valid_toml_passes_validation(self, tmp_path):
        """A clean scaffold writes valid TOML and proceeds without error."""
        _write_python_skeleton(tmp_path, "my-repo")

        pkg_name = _python_package_name("my-repo")
        assert (tmp_path / "pyproject.toml").exists()
        assert (tmp_path / "src" / pkg_name / "__init__.py").exists()
        assert (tmp_path / "tests" / "__init__.py").exists()

    def test_invalid_toml_raises_value_error(self, tmp_path, monkeypatch):
        """If the generated TOML is invalid — e.g. a stray corruption line —
        a ValueError is raised and no directories are created."""
        # Intercept the write to inject a broken file after write but before
        # validation. We patch the write to also append garbage.
        orig_write = Path.write_text

        def _corrupted_write(self, content, encoding=None):
            orig_write(self, content + '\nPE_CHECKING:"]\n', encoding=encoding)

        monkeypatch.setattr(Path, "write_text", _corrupted_write)

        with pytest.raises(ValueError, match="invalid TOML"):
            _write_python_skeleton(tmp_path, "my-repo")

        # No directories were created after the failed validation
        assert not (tmp_path / "src").exists()
        assert not (tmp_path / "tests").exists()


# ---------------------------------------------------------------------------
# _write_periodic_presence_files
# ---------------------------------------------------------------------------


def test_write_periodic_presence_files(tmp_path):
    """The scaffold seeds the new repo with name-only periodic presence
    files (audit + health) — this is what actually enables those agents,
    since enablement is per-repo file presence, not a repos.yaml block."""
    _write_periodic_presence_files(tmp_path)

    periodic_dir = tmp_path / ".robotsix-mill" / "periodic"
    audit = periodic_dir / "audit.yaml"
    health = periodic_dir / "health.yaml"
    assert audit.exists() and health.exists()
    assert yaml.safe_load(audit.read_text()) == {"name": "audit"}
    assert yaml.safe_load(health.read_text()) == {"name": "health"}


# ---------------------------------------------------------------------------
# _write_github_workflows
# ---------------------------------------------------------------------------


def test_write_github_workflows(tmp_path):
    """The scaffold writes reusable-workflow callers that reference the
    correct CROSS-REPO org (`damien-robotsix`, NOT `robotsix`) and grant
    the permissions the called workflows require — so generated callers are
    correct by construction and never trip a `startup_failure`."""
    _write_github_workflows(tmp_path)

    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    docs = tmp_path / ".github" / "workflows" / "docs.yml"
    assert ci.exists() and docs.exists()

    ci_text = ci.read_text()
    assert (
        "uses: damien-robotsix/robotsix-mill/.github/workflows/"
        "python-ci.yml@main" in ci_text
    )
    # Cross-repo form, NOT the local `./...` form mill uses for itself.
    assert "./.github/workflows/python-ci.yml" not in ci_text
    # Wrong org (`robotsix/...` instead of `damien-robotsix/...`) must never
    # appear as a `uses:` target.
    assert "uses: robotsix/robotsix-mill" not in ci_text
    ci_data = yaml.safe_load(ci_text)
    ci_perms = ci_data["jobs"]["ci"]["permissions"]
    assert ci_perms["contents"] == "read"
    assert ci_perms["security-events"] == "write"

    docs_text = docs.read_text()
    assert (
        "uses: damien-robotsix/robotsix-mill/.github/workflows/"
        "python-docs.yml@main" in docs_text
    )
    assert "uses: robotsix/robotsix-mill" not in docs_text
    docs_data = yaml.safe_load(docs_text)
    assert docs_data["jobs"]["deploy"]["permissions"]["contents"] == "write"


def test_non_python_scaffold_writes_no_workflows(tmp_path, monkeypatch):
    """A non-python scaffold writes NO `.github/workflows/` callers — the
    shared reusable workflows are `python-*` and don't apply."""
    from robotsix_mill.forge.base import RepoInfo

    settings = _make_settings(tmp_path)
    monkeypatch.setenv("MILL_REPOS_FILE", "")

    init_dest = {}

    def _fake_init_repo(dest, branch):
        init_dest["dest"] = dest
        dest.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "robotsix_mill.repo_scaffold.git_ops.init_repo", _fake_init_repo
    )
    monkeypatch.setattr(
        "robotsix_mill.repo_scaffold.git_ops.commit_all",
        lambda repo, message: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.repo_scaffold.git_ops.push",
        lambda repo, branch, remote_url, token: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.repo_scaffold.github_token",
        lambda s, repo_config=None: "fake-token",
    )
    monkeypatch.setattr(
        "robotsix_mill.repo_scaffold.shutil.rmtree",
        lambda path, ignore_errors=False: None,
    )

    repo_info = RepoInfo(
        id=1,
        name="my-cpp-repo",
        clone_url="https://github.com/x/my-cpp-repo.git",
        html_url="https://github.com/x/my-cpp-repo",
    )
    from robotsix_mill.repo_scaffold import _scaffold_initial_commit

    _scaffold_initial_commit(settings, repo_info, _make_params(language="cpp"))

    dest = init_dest["dest"]
    assert not (dest / ".github" / "workflows").exists()


# ---------------------------------------------------------------------------
# _file_implementation_followup
# ---------------------------------------------------------------------------


def test_file_implementation_followup_creates_buildout_ticket(tmp_path, monkeypatch):
    """After scaffolding an EMPTY repo, a build-out ticket is filed on the
    new repo's own board so the normal pipeline populates it. The spec is
    derived from the scaffold ticket's purpose (description — no marker to
    strip since markers are gone)."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import SourceKind
    from robotsix_mill.core.service import TicketService
    from robotsix_mill.forge.base import RepoInfo
    from robotsix_mill.repo_scaffold import _file_implementation_followup

    monkeypatch.setenv("MILL_REPOS_FILE", "")
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="robotsix-modules")

    repo_info = RepoInfo(
        id=1,
        name="robotsix-modules",
        clone_url="https://github.com/x/robotsix-modules.git",
        html_url="https://github.com/x/robotsix-modules",
    )
    scaffold_desc = "Extract the module-taxonomy schema into a standalone library.\n\n"
    fid = _file_implementation_followup(
        settings, repo_info, {"description": ""}, scaffold_desc
    )
    assert fid is not None

    svc = TicketService(settings, board_id="robotsix-modules")
    t = svc.get(fid)
    assert t is not None
    assert t.source == SourceKind.AGENT
    assert "robotsix-modules" in t.title
    body = svc.workspace(t).read_description()
    assert "module-taxonomy schema" in body
