"""Tests for ``robotsix_mill.repo_scaffold`` and the implement-stage guard clause."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import yaml

from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge.base import NotConfiguredError, RepoInfo
from robotsix_mill.repo_scaffold import (
    MARKER_KIND,
    _append_repo_config,
    _sanitize_repo_id,
    _write_periodic_presence_files,
    parse_new_repo_params,
    run_repo_scaffold,
)
from robotsix_mill.stages.base import Outcome, StageContext
from robotsix_mill.stages.implement import ImplementStage


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


def _new_repo_marker(**fields) -> str:
    """Build a ``new-repo`` extraction marker block for a ticket description."""
    defaults = {
        "name": "my-new-repo",
        "owner": "my-org",
        "private": True,
        "description": "A test repo",
        "language": "python",
    }
    defaults.update(fields)
    lines = ["<!-- meta-extraction-kind: new-repo"]
    for key in ("name", "owner", "private", "description", "language"):
        val = defaults[key]
        if isinstance(val, bool):
            val = str(val).lower()
        lines.append(f"  {key}: {val}")
    lines.append("-->")
    return "\n".join(lines)


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
# parse_new_repo_params
# ---------------------------------------------------------------------------


class TestParseNewRepoParams:
    def test_present_all_fields(self):
        desc = _new_repo_marker()
        params = parse_new_repo_params(desc)
        assert params is not None
        assert params["name"] == "my-new-repo"
        assert params["owner"] == "my-org"
        assert params["private"] is True
        assert params["description"] == "A test repo"
        assert params["language"] == "python"

    def test_present_minimal_fields(self):
        desc = """<!-- meta-extraction-kind: new-repo
  name: minimal-repo
-->"""
        params = parse_new_repo_params(desc)
        assert params is not None
        assert params["name"] == "minimal-repo"
        assert params["owner"] == ""
        assert params["private"] is True  # default
        assert params["description"] == ""
        assert params["language"] == "python"  # default

    def test_private_defaults_true_when_absent(self):
        desc = """<!-- meta-extraction-kind: new-repo
  name: my-repo
  owner: someone
-->"""
        params = parse_new_repo_params(desc)
        assert params is not None
        assert params["private"] is True

    def test_missing_marker(self):
        desc = "Just a regular ticket description"
        params = parse_new_repo_params(desc)
        assert params is None

    def test_malformed_yaml(self, caplog):
        desc = """<!-- meta-extraction-kind: new-repo
  name: [unclosed
-->"""
        with caplog.at_level(logging.WARNING):
            params = parse_new_repo_params(desc)
        assert params is None
        assert any("Failed to parse" in m for m in caplog.messages)

    def test_missing_name_field(self, caplog):
        desc = """<!-- meta-extraction-kind: new-repo
  owner: someone
-->"""
        with caplog.at_level(logging.WARNING):
            params = parse_new_repo_params(desc)
        assert params is None
        assert any("missing required 'name'" in m for m in caplog.messages)

    def test_partial_match_not_full_marker(self):
        """Marker kind pattern must be on the opening comment line."""
        desc = "<!-- some other comment -->\nname: x"
        params = parse_new_repo_params(desc)
        assert params is None


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

        # Register the mill repo so its Langfuse keys can be copied
        mill_cfg = _repo_cfg(
            "robotsix-mill",
            board_id="robotsix-mill",
            langfuse_public_key="pk-mill-123",
            langfuse_secret_key="sk-mill-456",
            langfuse_base_url="https://langfuse.example.com",
        )
        reg = ReposRegistry(repos={"robotsix-mill": mill_cfg})
        monkeypatch.setattr("robotsix_mill.repo_scaffold.get_repos_config", lambda: reg)

        repo_info = RepoInfo(
            id=42,
            name="my-new-repo",
            clone_url="https://github.com/my-org/my-new-repo.git",
            html_url="https://github.com/my-org/my-new-repo",
        )
        params = parse_new_repo_params(_new_repo_marker())
        assert params is not None

        _append_repo_config(repo_info, params, settings)

        # Read back and verify
        with open(repos_file, "r") as fh:
            data = yaml.safe_load(fh)

        repos = data["repos"]
        assert "my-new-repo" in repos
        entry = repos["my-new-repo"]

        assert entry["board_id"] == "my-new-repo"
        assert entry["langfuse"]["project_name"] == "my-new-repo"
        assert entry["langfuse"]["public_key"] == "pk-mill-123"
        assert entry["langfuse"]["secret_key"] == "sk-mill-456"
        assert entry["langfuse"]["base_url"] == "https://langfuse.example.com"
        assert entry["forge_remote_url"] == "https://github.com/my-org/my-new-repo.git"
        assert entry["language"] == "python"
        assert entry["test_command"] == "pytest -q"
        # Periodic config is NOT written to repos.yaml (the loader ignores
        # it); enablement is per-repo file presence in the new repo.
        assert "periodic" not in entry

        _reset_repos_config()

    def test_langfuse_keys_copied_from_mill_repo(self, tmp_path, monkeypatch):
        """Verify new stanza copies langfuse keys from the mill repo config."""
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))

        settings = _make_settings(
            tmp_path,
            trace_review_target_repo_id="robotsix-mill",
        )
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        mill_cfg = _repo_cfg(
            "robotsix-mill",
            board_id="robotsix-mill",
            langfuse_public_key="pk-custom",
            langfuse_secret_key="sk-custom",
            langfuse_base_url="https://lf.custom.com",
        )
        reg = ReposRegistry(repos={"robotsix-mill": mill_cfg})
        monkeypatch.setattr("robotsix_mill.repo_scaffold.get_repos_config", lambda: reg)

        repo_info = RepoInfo(
            id=1,
            name="new-repo",
            clone_url="https://github.com/x/new-repo.git",
            html_url="https://github.com/x/new-repo",
        )
        params = parse_new_repo_params(_new_repo_marker(name="new-repo"))
        assert params is not None

        _append_repo_config(repo_info, params, settings)

        with open(repos_file, "r") as fh:
            data = yaml.safe_load(fh)

        entry = data["repos"]["new-repo"]
        assert entry["langfuse"]["public_key"] == "pk-custom"
        assert entry["langfuse"]["secret_key"] == "sk-custom"
        assert entry["langfuse"]["base_url"] == "https://lf.custom.com"

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

        mill_cfg = _repo_cfg("robotsix-mill", board_id="robotsix-mill")
        reg = ReposRegistry(repos={"robotsix-mill": mill_cfg})
        monkeypatch.setattr("robotsix_mill.repo_scaffold.get_repos_config", lambda: reg)

        repo_info = RepoInfo(
            id=2,
            name="second-repo",
            clone_url="https://github.com/x/second-repo.git",
            html_url="https://github.com/x/second-repo",
        )
        params = parse_new_repo_params(_new_repo_marker(name="second-repo"))
        assert params is not None

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
        params = parse_new_repo_params(_new_repo_marker(name="x"))
        assert params is not None

        # Should not raise
        _append_repo_config(repo_info, params, settings)


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
        mill_cfg = _repo_cfg("robotsix-mill", board_id="robotsix-mill")
        reg = ReposRegistry(repos={"robotsix-mill": mill_cfg})
        monkeypatch.setattr("robotsix_mill.repo_scaffold.get_repos_config", lambda: reg)

        # Set up repos.yaml path
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))

        # Create ticket with new-repo marker
        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Create new-repo my-repo",
            description=_new_repo_marker(name="my-repo"),
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
        clone_calls = []
        commit_calls = []
        push_calls = []

        def _fake_clone(*, remote_url, dest, branch, token):
            clone_calls.append(
                {"remote_url": remote_url, "dest": dest, "branch": branch}
            )
            # Create the directory so subsequent writes work
            dest.mkdir(parents=True, exist_ok=True)

        def _fake_commit_all(repo, message):
            commit_calls.append({"repo": repo, "message": message})

        def _fake_push(repo, branch, remote_url, token):
            push_calls.append(
                {"repo": repo, "branch": branch, "remote_url": remote_url}
            )

        monkeypatch.setattr("robotsix_mill.repo_scaffold.git_ops.clone", _fake_clone)
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

        outcome = run_repo_scaffold(settings, ticket, forge, ctx)

        assert outcome.next_state == State.DONE
        forge.create_repo.assert_called_once_with(
            name="my-repo",
            owner="my-org",
            private=True,
            description="A test repo",
        )

        # git_ops.clone was called
        assert len(clone_calls) == 1
        assert clone_calls[0]["remote_url"] == "https://github.com/my-org/my-repo.git"
        assert clone_calls[0]["branch"] == settings.forge_target_branch

        # git_ops.commit_all was called
        assert len(commit_calls) == 1
        assert commit_calls[0]["message"] == "Initial scaffold"

        # git_ops.push was called
        assert len(push_calls) == 1
        assert push_calls[0]["branch"] == settings.forge_target_branch

        # Verify scaffold files were written in the clone dest
        dest = clone_calls[0]["dest"]
        assert (dest / "README.md").exists()
        assert (dest / "LICENSE").exists()
        assert (dest / "pyproject.toml").exists()
        assert (dest / "src" / "my-repo" / "__init__.py").exists()
        assert (dest / "tests" / "__init__.py").exists()

        # Verify repos.yaml was written
        assert repos_file.exists()
        with open(repos_file, "r") as fh:
            data = yaml.safe_load(fh)
        assert "my-repo" in data["repos"]

        _reset_repos_config()
        db.reset_engine()

    def test_not_configured_error_fallback(self, tmp_path, monkeypatch):
        """NotConfiguredError → BLOCKED with comment."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Create new-repo",
            description=_new_repo_marker(name="my-repo"),
        )
        ctx = _stage_context(settings, ticket)

        forge = MagicMock()
        forge.create_repo.side_effect = NotConfiguredError("disabled")

        # Capture add_comment calls
        add_comment_calls = []

        def _fake_add_comment(ticket_id, body, author="user", parent_id=None):
            add_comment_calls.append(
                {"ticket_id": ticket_id, "body": body, "author": author}
            )

        monkeypatch.setattr(ctx.service, "add_comment", _fake_add_comment)

        outcome = run_repo_scaffold(settings, ticket, forge, ctx)

        assert outcome.next_state == State.BLOCKED
        assert outcome.note == "Repo creation not configured"
        assert len(add_comment_calls) == 1
        assert "Repo creation is not configured" in add_comment_calls[0]["body"]
        assert "Manual steps" in add_comment_calls[0]["body"]

        db.reset_engine()

    def test_repo_already_exists_fallback(self, tmp_path, monkeypatch):
        """RuntimeError('already exists') → BLOCKED with comment."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Create new-repo",
            description=_new_repo_marker(name="my-repo"),
        )
        ctx = _stage_context(settings, ticket)

        forge = MagicMock()
        forge.create_repo.side_effect = RuntimeError("Repository already exists")

        add_comment_calls = []

        def _fake_add_comment(ticket_id, body, author="user", parent_id=None):
            add_comment_calls.append(
                {"ticket_id": ticket_id, "body": body, "author": author}
            )

        monkeypatch.setattr(ctx.service, "add_comment", _fake_add_comment)

        outcome = run_repo_scaffold(settings, ticket, forge, ctx)

        assert outcome.next_state == State.BLOCKED
        assert outcome.note == "Repo 'my-repo' already exists"
        assert len(add_comment_calls) == 1
        assert "already exists" in add_comment_calls[0]["body"].lower()

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
            description=_new_repo_marker(name="my-repo"),
        )
        ctx = _stage_context(settings, ticket)

        forge = MagicMock()
        forge.create_repo.side_effect = RuntimeError("Some other error")

        with pytest.raises(RuntimeError, match="Some other error"):
            run_repo_scaffold(settings, ticket, forge, ctx)

        db.reset_engine()


# ---------------------------------------------------------------------------
# ImplementStage guard clause
# ---------------------------------------------------------------------------


class TestGuardClause:
    def test_meta_ticket_with_marker_routes_to_scaffold(self, tmp_path, monkeypatch):
        """A meta-source ticket with new-repo marker triggers scaffold workflow."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Extract new repo",
            description=_new_repo_marker(name="extracted-lib"),
        )
        ctx = _stage_context(settings, ticket)

        # Mock the scaffold workflow so we can verify it was called
        scaffold_outcomes = []

        def _fake_run_scaffold(s, t, forge, ctx_):
            scaffold_outcomes.append({"ticket_id": t.id, "params": True})
            return Outcome(State.DONE)

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.ImplementStage._run_repo_scaffold",
            lambda ctx_, t, s_, p: _fake_run_scaffold(s_, t, None, ctx_),
        )

        stage = ImplementStage()
        outcome = stage.run(ticket, ctx)

        assert len(scaffold_outcomes) == 1
        assert scaffold_outcomes[0]["ticket_id"] == ticket.id
        assert outcome.next_state == State.DONE

        db.reset_engine()

    def test_non_meta_ticket_skips_guard(self, tmp_path, monkeypatch):
        """A non-META ticket passes through to normal implement."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="test-board")

        svc = TicketService(settings, board_id="test-board")
        ticket = _make_ticket(
            svc,
            title="Normal ticket",
            description=_new_repo_marker(name="some-repo"),
            source=SourceKind.USER,
        )
        ctx = _stage_context(settings, ticket, board_id="test-board")

        # Should hit the remote_url check (BLOCKED since no forge remote url)
        stage = ImplementStage()
        outcome = stage.run(ticket, ctx)

        # Not DONE (would be if scaffold ran) — it's BLOCKED due to no forge url
        assert outcome.next_state == State.BLOCKED
        assert "FORGE_REMOTE_URL" in (outcome.note or "")

        db.reset_engine()

    def test_meta_ticket_without_marker_enters_cross_repo_path(
        self, tmp_path, monkeypatch
    ):
        """A META ticket without the new-repo marker is NOT scaffolded;
        it enters the cross-repo meta workspace path. When triage fails
        (no LLM in unit tests), it BLOCKs with a clear note."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        svc = TicketService(settings, board_id="meta")
        ticket = _make_ticket(
            svc,
            title="Some other meta ticket",
            description="Just a normal ticket description, no marker",
            source=SourceKind.META,
        )
        ctx = _stage_context(settings, ticket, board_id="meta")

        stage = ImplementStage()
        outcome = stage.run(ticket, ctx)

        # Not scaffolded — reached the cross-repo meta workspace gate.
        # Without a real LLM the triage may fail or return no repos;
        # either path BLOCKs.
        assert outcome.next_state == State.BLOCKED
        assert "meta repo-triage failed" in (
            outcome.note or ""
        ) or "no repos could be cloned" in (outcome.note or "")

        db.reset_engine()


# ---------------------------------------------------------------------------
# MARKER_KIND constant
# ---------------------------------------------------------------------------


def test_marker_kind_constant():
    assert MARKER_KIND == "new-repo"


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
# _file_implementation_followup
# ---------------------------------------------------------------------------


def test_file_implementation_followup_creates_buildout_ticket(tmp_path, monkeypatch):
    """After scaffolding an EMPTY repo, a build-out ticket is filed on the
    new repo's own board so the normal pipeline populates it. The spec is
    derived from the scaffold ticket's purpose (description minus marker)."""
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
    scaffold_desc = (
        "Extract the module-taxonomy schema into a standalone library.\n\n"
        "<!-- meta-extraction-kind: new-repo\n  name: robotsix-modules\n-->"
    )
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
    assert "meta-extraction-kind" not in body


# ---------------------------------------------------------------------------
# build_new_repo_marker (the producer-side format owner)
# ---------------------------------------------------------------------------


def test_build_new_repo_marker_roundtrips():
    """build_new_repo_marker → parse_new_repo_params is an exact round-trip,
    including values that need YAML quoting (e.g. a colon in description)."""
    from robotsix_mill.repo_scaffold import build_new_repo_marker

    marker = build_new_repo_marker(
        "robotsix-modules",
        owner="damien-robotsix",
        private=False,
        description="Taxonomy validation: shared schema",
        language="python",
    )
    parsed = parse_new_repo_params("Spec body.\n\n" + marker)
    assert parsed == {
        "name": "robotsix-modules",
        "owner": "damien-robotsix",
        "private": False,
        "description": "Taxonomy validation: shared schema",
        "language": "python",
    }


def test_build_new_repo_marker_defaults_roundtrip():
    from robotsix_mill.repo_scaffold import build_new_repo_marker

    parsed = parse_new_repo_params(build_new_repo_marker("robotsix-foo"))
    assert parsed["name"] == "robotsix-foo"
    assert parsed["private"] is False
    assert parsed["language"] == "python"
