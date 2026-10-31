import contextlib
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    created = []

    def make(**env):
        from robotsix_mill.core import db

        db.reset_engine()
        # fake_sandbox replaces the (always-containerized) seam; no
        # Docker, no host execution.
        s = Settings(data_dir=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s, board_id="test-board")
        from robotsix_mill.core.service import TicketService

        svc = TicketService(s, board_id="test-board")
        created.append(s)
        from robotsix_mill.config import RepoConfig
        from robotsix_mill.stages import StageContext

        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    from robotsix_mill.core import db

    db.reset_engine()


def _ticket(ctx, title="Add feature", body="Please add feature.txt"):
    from robotsix_mill.core.states import State

    t = ctx.service.create(title, body)
    ctx.service.transition(t.id, State.READY)
    return ctx.service.get(t.id)


def _write_file_map(ctx, ticket, *files):
    """Write a minimal file_map.json for *ticket* listing *files*."""
    import json as _json

    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "file_map.json").write_text(
        _json.dumps([{"file": f, "note": "test"} for f in files]),
        encoding="utf-8",
    )


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_bare_repo(tmp_path: Path) -> str:
    """A throwaway local remote (file://) with a `main` branch — lets us
    exercise clone/branch/commit fully offline, no forge."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", "main")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


def _fake_agent(write: dict | None):
    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
            previous_attempt_summary,
        )  # signature must match the seam
        if write:
            for name, content in write.items():
                (Path(repo_dir) / name).write_text(content)
        return (
            "did the thing",
            list(write.keys()) if write else [],
            "",
            None,
            None,
            False,
            "",
        )

    return _run


# --- misc helper --------------------------------------------------------


def _clone_repo_to(ctx, remote_url, repo_dir):
    """Clone to *repo_dir* without the full stage machinery."""
    from robotsix_mill.scm.forge.auth import github_token
    from robotsix_mill.scm.vcs import git_ops

    if repo_dir.exists():
        import shutil

        shutil.rmtree(repo_dir)
    token = None
    with contextlib.suppress(RuntimeError):
        token = github_token(ctx.settings, repo_config=ctx.repo_config)
    git_ops.clone(remote_url, repo_dir, ctx.settings.forge_target_branch, token)
