"""Shared git fixtures for the ``test_git_ops_*`` modules."""

import os
import subprocess
from pathlib import Path


def _git(cwd, *args, env: dict[str, str] | None = None):
    """Run git in *cwd*. *env* overlays the ambient environment.

    The overlay exists so a test can set GIT_COMMITTER_* independently of
    the author — git has no ``committer.email`` config knob, and the
    post-push check inspects author and committer separately.
    """
    run_env = {**os.environ, **env} if env else None
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env=run_env,
    )


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
