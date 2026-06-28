import json
import subprocess
from pathlib import Path

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.deliver import DeliverStage
from robotsix_mill.stages import deliver as deliver_module
from robotsix_mill.vcs import git_ops


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def _bare(tmp_path) -> str:
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
    return f"file://{bare}", bare


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**env)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.get("FORGE_TOKEN")
    if ft is not None:
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(forge_token=ft)
    db.init_db(s, board_id="test-board")
    from robotsix_mill.config import RepoConfig

    return StageContext(
        settings=s,
        service=TicketService(s, board_id="test-board"),
        repo_config=RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
    )


def _ticket_with_branch(ctx, remote):
    """Simulate a finished implement: ticket in DELIVERABLE with a
    committed branch in its workspace clone."""
    t = ctx.service.create("add x", "do x")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    repo = ctx.service.workspace(t).dir / "repo"
    git_ops.clone(remote, repo, "main", None)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    (repo / "x.txt").write_text("done")
    git_ops.commit_all(repo, "impl")
    ctx.service.set_branch(t.id, branch)
    return ctx.service.get(t.id), branch


# --- owner/repo parsing -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/damien-robotsix/robotsix-mill.git",
        "https://github.com/damien-robotsix/robotsix-mill",
        "git@github.com:damien-robotsix/robotsix-mill.git",
    ],
)
def test_parse_owner_repo(url):
    assert github._parse_owner_repo(url) == ("damien-robotsix", "robotsix-mill")


def test_create_pr_posts_to_github_api(tmp_path, monkeypatch):
    import httpx

    calls = {}

    class FakeResp:
        status_code = 201
        text = ""

        def json(self):
            return {"html_url": "https://github.com/o/r/pull/7"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.update(url=url, headers=headers, json=json)
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(forge_token="tok")
    s = Settings(
        data_dir=str(tmp_path),
        FORGE_KIND="github",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    url = github.GitHubForge(s).open_merge_request(
        source_branch="mill/x", title="T", body="B"
    )
    assert url == "https://github.com/o/r/pull/7"
    assert calls["url"].endswith("/repos/o/r/pulls")
    assert calls["json"] == {
        "title": "T",
        "head": "mill/x",
        "base": "main",
        "body": "B",
    }
    assert calls["headers"]["Authorization"] == "Bearer tok"


# --- deliver guards (no external calls) --------------------------------


def test_blocked_without_forge_kind(tmp_path):
    ctx = _ctx(tmp_path, data_dir=str(tmp_path / "d0"))
    t = ctx.service.create("x", "y")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    out = DeliverStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.BLOCKED and "FORGE_KIND" in out.note


def test_auto_forge_kind_bypasses_none_guard(tmp_path):
    """forge_kind=auto with a valid remote_url bypasses the
    FORGE_KIND=none guard — it should NOT block with "FORGE_KIND"."""
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="auto",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        FORGE_TOKEN="tok",
    )
    t = ctx.service.create("x", "y")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    out = DeliverStage().run(ctx.service.get(t.id), ctx)
    # Should NOT be blocked due to FORGE_KIND — the "auto" value is
    # allowed through. It may block elsewhere (e.g. no workspace
    # branch), but the note must not contain the "FORGE_KIND not
    # configured" sentinel.
    assert "FORGE_KIND not configured" not in out.note


def test_blocked_without_token(tmp_path):
    remote, _ = _bare(tmp_path)
    ctx = _ctx(tmp_path, FORGE_KIND="github", FORGE_REMOTE_URL=remote)
    t = ctx.service.create("x", "y")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    out = DeliverStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.BLOCKED and "FORGE_TOKEN" in out.note


def test_blocked_without_branch(tmp_path):
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    t = ctx.service.create("x", "y")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    out = DeliverStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.BLOCKED and "no implemented branch" in out.note


# --- success + resumable failure ---------------------------------------


def test_success_pushes_and_opens_pr(tmp_path, monkeypatch):
    remote, bare = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    seen = {}

    def fake_pr(self, *, source_branch, title, body):
        seen.update(source_branch=source_branch, title=title)
        return "https://github.com/o/r/pull/42"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)
    t, branch = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)

    assert (
        out.next_state is State.IMPLEMENT_COMPLETE
    )  # PR opened, gates not checked yet
    assert "https://github.com/o/r/pull/42" in out.note
    assert seen["source_branch"] == branch
    assert t.id in seen["title"]
    # branch actually pushed to the bare remote
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        capture_output=True,
        text=True,
    ).stdout
    assert branch in refs
    assert (ctx.service.workspace(t).artifacts_dir / "deliver.md").exists()


def test_pr_api_error_blocks_resumable(tmp_path, monkeypatch):
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )

    def boom(self, *, source_branch, title, body):
        raise RuntimeError("GitHub PR create failed: 403")

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", boom)
    t, _ = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note and "403" in out.note


def test_pr_422_no_commits_routes_to_done(tmp_path, monkeypatch):
    """A 422 "No commits between" from the forge routes to DONE, not BLOCKED.

    The local branch_has_net_diff guard fail-opens when the workspace clone is
    absent or its origin/main ref is stale, so an empty branch can slip past it
    to the PR-create call. The forge's own emptiness verdict is authoritative —
    routing to DONE stops the infinite block-loop (every resume re-hits the
    identical 422) instead of stranding the ticket.
    """
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )

    def boom(self, *, source_branch, title, body):
        raise RuntimeError(
            'GitHub PR create failed: 422 {"message":"Validation Failed",'
            '"errors":[{"resource":"PullRequest","code":"custom",'
            '"message":"No commits between main and ' + source_branch + '"}]}'
        )

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", boom)
    # A branch WITH a real commit so the upstream net-diff guard passes and we
    # actually reach the PR-create call (where the forge disagrees).
    t, _ = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "no change needed" in out.note.lower()
    assert "no commits between" in out.note.lower()


def test_pr_non_422_error_still_blocks_resumable(tmp_path, monkeypatch):
    """A non-422 forge error must still BLOCK-resumable (not be swallowed)."""
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )

    def boom(self, *, source_branch, title, body):
        raise RuntimeError("GitHub PR create failed: 500 internal error")

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", boom)
    t, _ = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note and "500" in out.note


# --- zero-diff guard ----------------------------------------------------


def test_zero_diff_branch_routes_to_done_without_pr_call(tmp_path, monkeypatch):
    """When the feature branch has no commits vs origin/main, the guard
    skips the PR API call and routes to DONE (not BLOCKED) — by the
    time we reach deliver, the implement stage's own no-change gate
    has already filtered out the silent-failure case, so an empty
    branch here means "the spec is already satisfied; nothing to ship."
    """
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    pr_called = False

    def fake_pr(self, *, source_branch, title, body):
        nonlocal pr_called
        pr_called = True
        return "https://github.com/o/r/pull/99"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    # Create a ticket whose branch is identical to main (no new commits)
    t = ctx.service.create("noop", "nothing to do")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    repo = ctx.service.workspace(t).dir / "repo"
    git_ops.clone(remote, repo, "main", None)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    # Do NOT commit anything — branch HEAD == main, so diff is empty
    ctx.service.set_branch(t.id, branch)
    t = ctx.service.get(t.id)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in out.note.lower()
    assert not pr_called, "PR API must not be called for zero-diff branch"


def test_zero_diff_guard_happy_path_unaffected(tmp_path, monkeypatch):
    """The guard is a no-op when the branch has new commits — PR
    creation proceeds as before."""
    remote, bare = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    seen = {}

    def fake_pr(self, *, source_branch, title, body):
        seen.update(source_branch=source_branch, title=title)
        return "https://github.com/o/r/pull/42"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)
    t, branch = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "https://github.com/o/r/pull/42" in out.note
    assert seen["source_branch"] == branch
    assert t.id in seen["title"]


# --- cross-repo target (push to fork, PR fork→upstream) ----------------


def _ctx_cross(tmp_path, cct, **env):
    """Build a StageContext whose repo_config carries *cct*
    (a CrossRepoTarget)."""
    ctx = _ctx(tmp_path, **env)
    from robotsix_mill.config import RepoConfig

    rc = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        cross_repo_target=cct,
    )
    return StageContext(settings=ctx.settings, service=ctx.service, repo_config=rc)


def test_cross_repo_pushes_to_fork_and_opens_fork_to_upstream_pr(tmp_path, monkeypatch):
    """With a cross_repo_target, deliver pushes to fork_remote_url and
    opens the PR with head_repo set to the fork's owner/repo."""
    from robotsix_mill.config import CrossRepoTarget

    remote, _ = _bare(tmp_path)
    cct = CrossRepoTarget(
        upstream_remote_url="https://github.com/up/r.git",
        fork_remote_url="https://github.com/fork-owner/r.git",
        base_branch="main",
    )
    ctx = _ctx_cross(
        tmp_path,
        cct,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )

    pushed = {}

    def fake_push(repo_dir, branch, remote_url, token):
        pushed.update(remote_url=remote_url, branch=branch)

    seen = {}

    def fake_pr(self, *, source_branch, title, body, head_repo=None):
        seen.update(head_repo=head_repo, source_branch=source_branch)
        return "https://github.com/up/r/pull/5"

    monkeypatch.setattr(git_ops, "push", fake_push)
    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)
    t, branch = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "https://github.com/up/r/pull/5" in out.note
    # Pushed to the fork, not the clone remote.
    assert pushed["remote_url"] == "https://github.com/fork-owner/r.git"
    assert pushed["branch"] == branch
    # PR opened with the fork as head_repo.
    assert seen["head_repo"] == "fork-owner/r"
    assert seen["source_branch"] == branch


def test_cross_repo_auto_fork_forks_before_push(tmp_path, monkeypatch):
    """auto_fork=True ensures the fork via Forge.fork_repo() before push."""
    from robotsix_mill.config import CrossRepoTarget
    from robotsix_mill.forge.base import RepoInfo

    remote, _ = _bare(tmp_path)
    cct = CrossRepoTarget(
        upstream_remote_url="https://github.com/up/r.git",
        fork_remote_url="https://github.com/fork-owner/r.git",
        auto_fork=True,
    )
    ctx = _ctx_cross(
        tmp_path,
        cct,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )

    events = []

    def fake_push(repo_dir, branch, remote_url, token):
        events.append(("push", remote_url))

    def fake_fork(self, *, source_owner, source_repo, target_namespace=None):
        events.append(("fork", source_owner, source_repo))
        return RepoInfo(
            id=1,
            name=source_repo,
            clone_url="https://github.com/fork-owner/r.git",
            html_url="https://github.com/fork-owner/r",
        )

    def fake_pr(self, *, source_branch, title, body, head_repo=None):
        return "https://github.com/up/r/pull/9"

    monkeypatch.setattr(git_ops, "push", fake_push)
    monkeypatch.setattr(github.GitHubForge, "fork_repo", fake_fork)
    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)
    t, _ = _ticket_with_branch(ctx, remote)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    # Fork happened, and it happened BEFORE the push.
    assert ("fork", "up", "r") in events
    assert events.index(("fork", "up", "r")) < events.index(
        ("push", "https://github.com/fork-owner/r.git")
    )


# --- multi-repo delivery (meta-board tickets, N ≥ 1) -------------------


def _bare_in(parent: Path, name: str) -> tuple[str, Path]:
    """Create a throwaway bare remote under ``parent/name/`` with a
    ``main`` branch. Returns ``(file:// url, bare path)``."""
    sub = parent / name
    sub.mkdir(parents=True)
    seed = sub / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", "main")
    bare = sub / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}", bare


def _install_repos_registry(entries: list[tuple[str, str]]) -> None:
    """Populate ``config._repos_config`` for the multi-repo tests.

    ``entries`` is a list of ``(repo_id, forge_remote_url)`` pairs.
    """
    from robotsix_mill.config import RepoConfig, ReposRegistry, _reset_repos_config
    import robotsix_mill.config as _cfg

    _reset_repos_config()
    _cfg._repos_config = ReposRegistry(
        repos={
            rid: RepoConfig(
                repo_id=rid,
                board_id="meta",
                langfuse_project_name=f"p-{rid}",
                langfuse_public_key=f"pk-{rid}",
                langfuse_secret_key=f"sk-{rid}",
                forge_remote_url=url,
            )
            for rid, url in entries
        }
    )


@pytest.fixture(autouse=True)
def _reset_repos_registry_after_each_test():
    """Drop any test-installed ReposRegistry so module-global state
    never leaks between tests."""
    yield
    from robotsix_mill.config import _reset_repos_config

    _reset_repos_config()


def _make_meta_ticket(ctx) -> object:
    """Create a meta ticket in DELIVERABLE state."""
    t = ctx.service.create("Cross-repo feature", "do x in many repos")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    return ctx.service.get(t.id)


def _make_repo_clone(
    ws_dir: Path,
    repo_id: str,
    remote_url: str,
    branch: str,
    *,
    with_commit: bool,
) -> dict:
    """Clone *remote_url* into ``ws_dir/repos/<repo_id>``, create
    *branch*, optionally add a commit, and return the touched-repos
    manifest entry shape for it."""
    repos_dir = ws_dir / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo = repos_dir / repo_id
    git_ops.clone(remote_url, repo, "main", None)
    git_ops.create_branch(repo, branch)
    if with_commit:
        (repo / f"feature-{repo_id}.txt").write_text("done")
        git_ops.commit_all(repo, f"impl {repo_id}")
    return {"repo_id": repo_id, "branch": branch, "repo_path": str(repo)}


def _write_touched_repos(ctx, ticket, entries: list[dict]) -> None:
    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "touched_repos.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )


def test_multi_repo_happy_path_opens_one_pr_per_repo(tmp_path, monkeypatch):
    """AC1: two touched repos, both ahead of main → exactly two PRs,
    pr_urls.json + deliver.md list both."""
    remote_a, bare_a = _bare_in(tmp_path, "a")
    remote_b, bare_b = _bare_in(tmp_path, "b")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote_a,
        FORGE_TOKEN="t",
    )
    _install_repos_registry(
        [("repo-a", remote_a), ("repo-b", remote_b)],
    )

    calls = []

    def fake_pr(self, *, source_branch, title, body):
        # Distinguish PRs by the per-repo forge_remote_url.
        rurl = self._remote_url
        calls.append({"source_branch": source_branch, "remote": rurl, "title": title})
        if rurl == remote_a:
            return "https://github.com/o/a/pull/1"
        if rurl == remote_b:
            return "https://github.com/o/b/pull/2"
        raise AssertionError(f"unexpected remote: {rurl}")

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    entries = [
        _make_repo_clone(ws.dir, "repo-a", remote_a, branch, with_commit=True),
        _make_repo_clone(ws.dir, "repo-b", remote_b, branch, with_commit=True),
    ]
    _write_touched_repos(ctx, t, entries)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "https://github.com/o/a/pull/1" in out.note
    assert "https://github.com/o/b/pull/2" in out.note
    # open_merge_request invoked once per repo with the right branch.
    assert len(calls) == 2
    branches_called = {c["source_branch"] for c in calls}
    assert branches_called == {branch}
    remotes_called = {c["remote"] for c in calls}
    assert remotes_called == {remote_a, remote_b}
    for c in calls:
        assert t.id in c["title"]

    # pr_urls.json lists both PRs.
    pr_path = ws.artifacts_dir / "pr_urls.json"
    assert pr_path.exists()
    pr_list = json.loads(pr_path.read_text(encoding="utf-8"))
    assert len(pr_list) == 2
    assert {e["repo_id"] for e in pr_list} == {"repo-a", "repo-b"}
    assert {e["url"] for e in pr_list} == {
        "https://github.com/o/a/pull/1",
        "https://github.com/o/b/pull/2",
    }
    for e in pr_list:
        assert e["branch"] == branch

    # deliver.md lists both repos and PRs.
    deliver_md = (ws.artifacts_dir / "deliver.md").read_text(encoding="utf-8")
    assert "repo-a" in deliver_md
    assert "repo-b" in deliver_md
    assert "https://github.com/o/a/pull/1" in deliver_md
    assert "https://github.com/o/b/pull/2" in deliver_md


def test_empty_touched_repos_routes_to_done_without_forge(tmp_path, monkeypatch):
    """AC3: touched_repos.json == [] → DONE, no push, no PR, no pr_urls.json."""
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )

    push_called = False
    pr_called = False

    def fake_push(*args, **kwargs):
        nonlocal push_called
        push_called = True

    def fake_pr(self, *, source_branch, title, body):
        nonlocal pr_called
        pr_called = True
        return "https://github.com/o/r/pull/99"

    monkeypatch.setattr(git_ops, "push", fake_push)
    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    t = _make_meta_ticket(ctx)
    _write_touched_repos(ctx, t, [])

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in out.note.lower()
    assert not push_called, "git push must not be called for empty manifest"
    assert not pr_called, "open_merge_request must not be called for empty manifest"
    ws = ctx.service.workspace(t)
    assert not (ws.artifacts_dir / "pr_urls.json").exists()


def test_per_repo_ahead_guard_skips_repo_without_commits(tmp_path, monkeypatch):
    """AC4: repo with no commits ahead → skipped; the other repo gets a PR."""
    remote_a, _ = _bare_in(tmp_path, "a")
    remote_b, _ = _bare_in(tmp_path, "b")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote_a,
        FORGE_TOKEN="t",
    )
    _install_repos_registry(
        [("repo-a", remote_a), ("repo-b", remote_b)],
    )

    calls = []

    def fake_pr(self, *, source_branch, title, body):
        calls.append({"remote": self._remote_url, "source_branch": source_branch})
        return "https://github.com/o/a/pull/1"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    entries = [
        _make_repo_clone(ws.dir, "repo-a", remote_a, branch, with_commit=True),
        _make_repo_clone(ws.dir, "repo-b", remote_b, branch, with_commit=False),
    ]
    _write_touched_repos(ctx, t, entries)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    # Only repo-a's PR was opened.
    assert len(calls) == 1
    assert calls[0]["remote"] == remote_a

    pr_path = ws.artifacts_dir / "pr_urls.json"
    assert pr_path.exists()
    pr_list = json.loads(pr_path.read_text(encoding="utf-8"))
    assert len(pr_list) == 1
    assert pr_list[0]["repo_id"] == "repo-a"

    deliver_md = (ws.artifacts_dir / "deliver.md").read_text(encoding="utf-8")
    assert "repo-a" in deliver_md
    assert "repo-b" in deliver_md
    assert "SKIPPED" in deliver_md


def test_all_repos_skipped_routes_to_done_without_pr_urls(tmp_path, monkeypatch):
    """AC5: every touched repo has no commits ahead → DONE, no
    pr_urls.json written, forge never called."""
    remote_a, _ = _bare_in(tmp_path, "a")
    remote_b, _ = _bare_in(tmp_path, "b")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote_a,
        FORGE_TOKEN="t",
    )
    _install_repos_registry(
        [("repo-a", remote_a), ("repo-b", remote_b)],
    )

    pr_called = False

    def fake_pr(self, *, source_branch, title, body):
        nonlocal pr_called
        pr_called = True
        return "https://github.com/o/r/pull/99"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    entries = [
        _make_repo_clone(ws.dir, "repo-a", remote_a, branch, with_commit=False),
        _make_repo_clone(ws.dir, "repo-b", remote_b, branch, with_commit=False),
    ]
    _write_touched_repos(ctx, t, entries)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in out.note.lower()
    assert not pr_called, "PR must not be called when every touched repo is skipped"
    assert not (ws.artifacts_dir / "pr_urls.json").exists()


def test_mid_loop_pr_failure_blocks_and_preserves_partial_manifest(
    tmp_path, monkeypatch
):
    """AC6: three repos, third PR raises → BLOCKED resumable, pr_urls.json
    contains exactly the first two entries (atomic-replace after each PR)."""
    remote_a, _ = _bare_in(tmp_path, "a")
    remote_b, _ = _bare_in(tmp_path, "b")
    remote_c, _ = _bare_in(tmp_path, "c")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote_a,
        FORGE_TOKEN="t",
    )
    _install_repos_registry(
        [("repo-a", remote_a), ("repo-b", remote_b), ("repo-c", remote_c)],
    )

    def fake_pr(self, *, source_branch, title, body):
        rurl = self._remote_url
        if rurl == remote_a:
            return "https://github.com/o/a/pull/1"
        if rurl == remote_b:
            return "https://github.com/o/b/pull/2"
        if rurl == remote_c:
            raise RuntimeError("GitHub PR create failed: 500 forced")
        raise AssertionError(f"unexpected remote: {rurl}")

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    entries = [
        _make_repo_clone(ws.dir, "repo-a", remote_a, branch, with_commit=True),
        _make_repo_clone(ws.dir, "repo-b", remote_b, branch, with_commit=True),
        _make_repo_clone(ws.dir, "repo-c", remote_c, branch, with_commit=True),
    ]
    _write_touched_repos(ctx, t, entries)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note.lower()
    assert "repo-c" in out.note

    pr_path = ws.artifacts_dir / "pr_urls.json"
    assert pr_path.exists(), "partial pr_urls.json must be preserved on mid-loop fail"
    pr_list = json.loads(pr_path.read_text(encoding="utf-8"))
    assert len(pr_list) == 2
    repo_ids = [e["repo_id"] for e in pr_list]
    assert repo_ids == ["repo-a", "repo-b"]


def test_unknown_repo_id_is_blocked_resumable_not_crash(tmp_path, monkeypatch):
    """AC7: touched_repos.json names a repo that's not in the registry →
    BLOCKED resumable, no crash."""
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    # Empty registry — any repo_id lookup will raise ConfigError.
    _install_repos_registry([])

    pr_called = False

    def fake_pr(self, *, source_branch, title, body):
        nonlocal pr_called
        pr_called = True
        return "https://github.com/o/r/pull/99"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    entries = [
        _make_repo_clone(ws.dir, "ghost-repo", remote, branch, with_commit=True),
    ]
    _write_touched_repos(ctx, t, entries)

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "ghost-repo" in out.note
    assert not pr_called


def test_missing_branch_in_touched_repo_is_blocked_resumable(tmp_path, monkeypatch):
    """AC8: touched_repos.json points at a repo whose branch no longer
    exists (e.g. someone checked out main after implement) → BLOCKED
    resumable, with 're-run implement' in the note."""
    remote, _ = _bare_in(tmp_path, "a")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    _install_repos_registry([("repo-a", remote)])

    pr_called = False

    def fake_pr(self, *, source_branch, title, body):
        nonlocal pr_called
        pr_called = True
        return "https://github.com/o/r/pull/99"

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    entry = _make_repo_clone(ws.dir, "repo-a", remote, branch, with_commit=True)
    # Simulate someone resetting the workspace — branch no longer exists.
    repo_dir = Path(entry["repo_path"])
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", "-q", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "branch", "-D", branch],
        check=True,
        capture_output=True,
    )
    _write_touched_repos(ctx, t, [entry])

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "re-run implement" in out.note.lower()
    assert "repo-a" in out.note
    assert not pr_called


# --- meta-triage fallback guard (new top-level file misroute) ----------


def _write_meta_triage(ctx, ticket, *, fallback: bool, repo_ids: list[str]) -> None:
    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "meta_triage.json").write_text(
        json.dumps({"repo_ids": repo_ids, "fallback": fallback}, indent=2),
        encoding="utf-8",
    )


def test_meta_triage_fallback_blocks_new_top_level_files(tmp_path, monkeypatch):
    """When triage fell back to clone-everything (no repo matched) and the
    branch adds brand-new top-level files, deliver BLOCKS instead of
    merging them into an arbitrarily-chosen primary repo."""
    remote, _ = _bare_in(tmp_path, "a")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    _install_repos_registry([("repo-a", remote)])

    pr_called = False
    push_called = False

    def fake_pr(self, *, source_branch, title, body):
        nonlocal pr_called
        pr_called = True
        return "https://github.com/o/r/pull/99"

    def fake_push(*a, **k):
        nonlocal push_called
        push_called = True

    monkeypatch.setattr(github.GitHubForge, "open_merge_request", fake_pr)
    monkeypatch.setattr(git_ops, "push", fake_push)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    # _make_repo_clone adds a brand-new top-level file (feature-<id>.txt).
    entry = _make_repo_clone(ws.dir, "repo-a", remote, branch, with_commit=True)
    _write_touched_repos(ctx, t, [entry])
    _write_meta_triage(ctx, t, fallback=True, repo_ids=["repo-a"])

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "could not be determined" in out.note
    assert "feature-repo-a.txt" in out.note
    assert not pr_called, "must not open a PR for misrouted new files"
    assert not push_called, "must not push for misrouted new files"


def test_genuine_all_repos_ticket_still_delivers_new_files(tmp_path, monkeypatch):
    """A genuine all-repos ticket (triage matched → fallback=False) still
    delivers brand-new top-level files normally."""
    remote, _ = _bare_in(tmp_path, "a")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    _install_repos_registry([("repo-a", remote)])

    monkeypatch.setattr(
        github.GitHubForge,
        "open_merge_request",
        lambda self, *, source_branch, title, body: "https://github.com/o/a/pull/1",
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    entry = _make_repo_clone(ws.dir, "repo-a", remote, branch, with_commit=True)
    _write_touched_repos(ctx, t, [entry])
    _write_meta_triage(ctx, t, fallback=False, repo_ids=["repo-a"])

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "https://github.com/o/a/pull/1" in out.note


def test_meta_triage_fallback_allows_non_top_level_files(tmp_path, monkeypatch):
    """Fallback guard is scoped to brand-new TOP-LEVEL files: a branch
    that only adds nested files (e.g. under ``docs/``) still delivers."""
    remote, _ = _bare_in(tmp_path, "a")
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    _install_repos_registry([("repo-a", remote)])

    monkeypatch.setattr(
        github.GitHubForge,
        "open_merge_request",
        lambda self, *, source_branch, title, body: "https://github.com/o/a/pull/2",
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ws = ctx.service.workspace(t)
    repo = ws.dir / "repos" / "repo-a"
    repo.parent.mkdir(parents=True, exist_ok=True)
    git_ops.clone(remote, repo, "main", None)
    git_ops.create_branch(repo, branch)
    (repo / "docs").mkdir()
    (repo / "docs" / "design.md").write_text("nested design doc")
    git_ops.commit_all(repo, "impl nested")
    entry = {"repo_id": "repo-a", "branch": branch, "repo_path": str(repo)}
    _write_touched_repos(ctx, t, [entry])
    _write_meta_triage(ctx, t, fallback=True, repo_ids=["repo-a"])

    out = DeliverStage().run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "https://github.com/o/a/pull/2" in out.note


# --- lockfile regen tests -----------------------------------------------


def test_lockfile_regen_uv_lock_called_when_pyproject_changed(tmp_path, monkeypatch):
    """Regen is called when pyproject.toml is in the net diff and uv.lock exists."""
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    t = ctx.service.create("update deps", "bump deps")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    repo = ctx.service.workspace(t).dir / "repo"
    git_ops.clone(remote, repo, "main", None)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    # Modify pyproject.toml and create uv.lock
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    (repo / "uv.lock").write_text("# lockfile\n")
    git_ops.commit_all(repo, "add deps")
    ctx.service.set_branch(t.id, branch)
    t = ctx.service.get(t.id)

    regen_calls = []
    monkeypatch.setattr(
        deliver_module,
        "_regen_uv_lock",
        lambda rd, tid: regen_calls.append(tid),
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "open_merge_request",
        lambda self, *, source_branch, title, body: "https://github.com/o/r/pull/1",
    )

    DeliverStage().run(t, ctx)
    assert len(regen_calls) == 1


def test_lockfile_regen_not_called_when_manifest_unchanged(tmp_path, monkeypatch):
    """No regen when the branch only touches a non-manifest file."""
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    t = ctx.service.create("doc update", "update readme")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    repo = ctx.service.workspace(t).dir / "repo"
    git_ops.clone(remote, repo, "main", None)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    (repo / "README.md").write_text("updated\n")
    (repo / "uv.lock").write_text("# lock\n")
    git_ops.commit_all(repo, "docs")
    ctx.service.set_branch(t.id, branch)
    t = ctx.service.get(t.id)

    uv_calls = []
    npm_calls = []
    monkeypatch.setattr(
        deliver_module,
        "_regen_uv_lock",
        lambda rd, tid: uv_calls.append(tid),
    )
    monkeypatch.setattr(
        deliver_module,
        "_regen_npm_lock",
        lambda rd, tid: npm_calls.append(tid),
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "open_merge_request",
        lambda self, *, source_branch, title, body: "https://github.com/o/r/pull/2",
    )

    DeliverStage().run(t, ctx)
    assert len(uv_calls) == 0
    assert len(npm_calls) == 0


def test_lockfile_regen_skipped_when_lockfile_absent(tmp_path, monkeypatch):
    """No regen when pyproject.toml changed but uv.lock does not exist."""
    remote, _ = _bare(tmp_path)
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_REMOTE_URL=remote,
        FORGE_TOKEN="t",
    )
    t = ctx.service.create("add dep", "add dep")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DELIVERABLE)
    repo = ctx.service.workspace(t).dir / "repo"
    git_ops.clone(remote, repo, "main", None)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    # uv.lock is deliberately absent
    git_ops.commit_all(repo, "add pyproject.toml")
    ctx.service.set_branch(t.id, branch)
    t = ctx.service.get(t.id)

    regen_calls = []
    monkeypatch.setattr(
        deliver_module,
        "_regen_uv_lock",
        lambda rd, tid: regen_calls.append(tid),
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "open_merge_request",
        lambda self, *, source_branch, title, body: "https://github.com/o/r/pull/3",
    )

    DeliverStage().run(t, ctx)
    assert len(regen_calls) == 0


def test_regen_uv_lock_warn_and_proceed_on_failure(tmp_path, monkeypatch, caplog):
    """uv lock failure logs a WARNING and does NOT raise or call commit_file."""
    import logging
    from subprocess import CompletedProcess

    # Create a bare git repo so commit_file can be called if needed
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    # Create uv.lock so commit_file would have something to stage
    (repo / "uv.lock").write_text("# lock\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "uv.lock"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "uv" and cmd[1] == "lock":
            return CompletedProcess(cmd, 1, stdout="", stderr="simulated failure")
        # Fall back to real subprocess for other calls (git ops in commit_file)
        return subprocess.run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    commit_calls = []
    monkeypatch.setattr(git_ops, "commit_file", lambda *a, **k: commit_calls.append(1))

    with caplog.at_level(logging.WARNING):
        deliver_module._regen_uv_lock(repo, "test-ticket-id")

    # Must not raise
    assert len(commit_calls) == 0
    assert any("uv lock failed" in record.message for record in caplog.records)
