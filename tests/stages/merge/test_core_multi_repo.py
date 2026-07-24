import contextlib
import json

import pytest

from robotsix_mill.agents.rebasing import RebaseResult
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages.merge import MergeStage
from robotsix_mill.vcs.git_ops import PostPushResult, ReconcileResult

from tests.stages.merge.test_core import _gh, _human_mr_approval


# ============================================================
# Multi-repo PR aggregation
# ============================================================


def _install_multirepo_registry(entries: list[tuple[str, str]]) -> None:
    """Populate the global ``_repos_config`` for multi-repo tests."""
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
def _reset_multirepo_registry_after_each_test():
    """Drop any test-installed ReposRegistry so module-global state
    never leaks between tests."""
    yield
    from robotsix_mill.config import _reset_repos_config

    _reset_repos_config()


def _write_pr_urls(ctx, ticket, entries: list[dict]) -> None:
    """Write a ``pr_urls.json`` manifest into the ticket's workspace."""
    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "pr_urls.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )


def _make_meta_ticket(ctx, *, state=State.IMPLEMENT_COMPLETE):
    """Create a ticket and transition it to *state* (default
    ``IMPLEMENT_COMPLETE`` — the multi-repo aggregator's first
    polling state)."""
    t = ctx.service.create("Cross-repo feature", "do x in many repos")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    if state is not State.IMPLEMENT_COMPLETE:
        ctx.service.transition(t.id, state)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _route_by_remote(
    monkeypatch,
    *,
    pr_responses: dict,
    ci_responses: dict | None = None,
    pr_by_url_responses: dict | None = None,
):
    """Monkeypatch the GitHub forge's ``pr_status`` + ``check_status``
    (and optionally ``pr_status_by_url``) so each call returns the
    response keyed by ``self._remote_url``.

    *pr_responses* / *ci_responses* / *pr_by_url_responses* are
    ``{remote_url: response | Exception}``.  A value can be a callable
    taking no args, an Exception (raised), or a plain dict / None
    (returned).
    """
    seen_pr: list[dict] = []
    seen_ci: list[dict] = []

    def fake_pr_status(self, *, source_branch):
        seen_pr.append({"remote": self._remote_url, "branch": source_branch})
        resp = pr_responses.get(self._remote_url)
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(github.GitHubForge, "pr_status", fake_pr_status)

    if pr_by_url_responses is not None:

        def fake_pr_status_by_url(self, *, url):
            resp = pr_by_url_responses.get(self._remote_url)
            if callable(resp) and not isinstance(resp, Exception):
                resp = resp()
            if isinstance(resp, Exception):
                raise resp
            return resp

        monkeypatch.setattr(
            github.GitHubForge, "pr_status_by_url", fake_pr_status_by_url
        )

    if ci_responses is not None:

        def fake_check_status(self, *, source_branch):
            seen_ci.append({"remote": self._remote_url, "branch": source_branch})
            resp = ci_responses.get(self._remote_url)
            if isinstance(resp, Exception):
                raise resp
            return resp

        monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)

    return seen_pr, seen_ci


def test_multi_repo_all_prs_merged_transitions_to_done(tmp_path, monkeypatch):
    """All N per-repo PRs merged → DONE; ``merge.md`` is the multi-line
    multi-repo header with one entry per repo."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "https://github.com/o/a/pull/1" in out.note
    assert "https://github.com/o/b/pull/2" in out.note

    merge_md = (ctx.service.workspace(t).artifacts_dir / "merge.md").read_text(
        encoding="utf-8"
    )
    assert merge_md.startswith("# Merge (multi-repo)")
    assert "repo-a" in merge_md
    assert "repo-b" in merge_md
    assert "https://github.com/o/a/pull/1" in merge_md
    assert "https://github.com/o/b/pull/2" in merge_md


def test_multi_repo_all_prs_merged_via_url_fallback_transitions_to_done(
    tmp_path, monkeypatch
):
    """Head branches auto-deleted on merge → branch-keyed ``pr_status``
    returns ``None`` for every repo, but the URL-keyed fallback reports
    each PR merged → DONE with the recorded URLs in ``out.note`` and
    ``merge.md``."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        # Branch-keyed lookup is empty (head branch auto-deleted).
        pr_responses={remote_a: None, remote_b: None},
        # URL-keyed fallback resolves the merged PRs.
        pr_by_url_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "https://github.com/o/a/pull/1" in out.note
    assert "https://github.com/o/b/pull/2" in out.note

    merge_md = (ctx.service.workspace(t).artifacts_dir / "merge.md").read_text(
        encoding="utf-8"
    )
    assert merge_md.startswith("# Merge (multi-repo)")
    assert "https://github.com/o/a/pull/1" in merge_md
    assert "https://github.com/o/b/pull/2" in merge_md


def test_multi_repo_url_fallback_partial_merge_stays_same_state(tmp_path, monkeypatch):
    """One repo's branch-keyed ``pr_status`` is ``None`` but the URL-keyed
    fallback reports it merged; the other is open with green CI →
    same-state no-op (no premature DONE)."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            # repo-a: head branch gone → None (falls back to URL lookup).
            remote_a: None,
            # repo-b: still open + mergeable.
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
            },
        },
        pr_by_url_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
        },
        ci_responses={
            remote_b: {"conclusion": "success", "failing": []},
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    # repo-a merged (via fallback), repo-b green but not eligible →
    # surface HUMAN_MR_APPROVAL so a human can merge the remaining PR.
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert not (ctx.service.workspace(t).artifacts_dir / "merge.md").exists()


def test_multi_repo_partial_merge_stays_same_state(tmp_path, monkeypatch):
    """One PR merged, one PR open with green CI → same-state no-op."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_b: {"conclusion": "success", "failing": []},
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    # repo-a merged, repo-b green but not eligible → surface
    # HUMAN_MR_APPROVAL so a human can merge the remaining PR.
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert not (ctx.service.workspace(t).artifacts_dir / "merge.md").exists()


def test_multi_repo_one_pr_closed_unmerged_blocks(tmp_path, monkeypatch):
    """One PR merged, one PR closed-unmerged → BLOCKED with repo_id + url."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": False,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "repo-b" in out.note
    assert "https://github.com/o/b/pull/2" in out.note


def test_multi_repo_conflicting_with_clone_runs_rebase(tmp_path, monkeypatch):
    """A conflicting repo WITH a workspace clone runs the rebase agent on that
    repo's clone, force-pushes the rebased branch to the per-repo remote, and
    re-polls (same state) — the multi-repo rebase auto-recovery."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": False,
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    captured = {}
    from robotsix_mill.stages import merge as merge_mod

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory, remote_url=None, token=None
    ):
        captured["repo_dir"] = repo_dir
        captured["branch"] = branch
        captured["target"] = target

        class _R:
            status = "DONE"
            updated_memory = ""

        return _R()

    monkeypatch.setattr(merge_mod, "run_rebase_agent", fake_rebase)
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # Reconcile is exercised separately; this test targets the
    # rebase/ci-fix flow, so treat the PR branch as in sync.
    monkeypatch.setattr(
        merge_mod.git_ops,
        "reconcile_with_remote_pr",
        lambda *a, **k: merge_mod.git_ops.ReconcileResult.SYNCED,
    )
    monkeypatch.setattr(merge_mod.git_ops, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {}
    monkeypatch.setattr(
        merge_mod.git_ops,
        "post_push_check",
        lambda repo, branch, target, remote_url, token: (
            pushed.update({"branch": branch, "remote": remote_url})
            or PostPushResult.PASS
        ),
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert captured["repo_dir"].endswith("repos/repo-b")
    assert captured["branch"] == branch
    assert pushed["branch"] == branch
    assert pushed["remote"] == remote_b
    counter = ctx.service.workspace(t).artifacts_dir / "rebase_repo-b.count"
    assert merge_mod._read_counter(counter) == 0


def test_multi_repo_conflicting_without_clone_blocks(tmp_path, monkeypatch):
    """A conflicting repo whose clone is missing → BLOCKED naming the repo."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": False,
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # No clone materialised for repo-b.
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "clone for repo-b missing — re-run implement" in out.note


def test_multi_repo_rebase_attempt_cap_blocks(tmp_path, monkeypatch):
    """Exhausting the per-repo rebase attempt counter → BLOCKED naming the
    repo + attempt count, and resets the counter for a future resume."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": False,
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    from robotsix_mill.stages import merge as merge_mod

    # Pre-seed the counter at the cap so the next attempt exceeds it.
    counter = ctx.service.workspace(t).artifacts_dir / "rebase_repo-b.count"
    merge_mod._write_counter(counter, ctx.settings.rebase_max_attempts)

    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "repo-b" in out.note
    assert "attempt" in out.note
    assert merge_mod._read_counter(counter) == 0


def test_multi_repo_one_pr_failing_ci_missing_clone_blocks(tmp_path, monkeypatch):
    """One PR green, one PR open + CI failure → the aggregator routes the
    failing repo to the inline CI-fix path; with no workspace clone it
    BLOCKS asking for re-implement (rather than the old immediate block)."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/a/pull/1",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "failure", "failing": []},
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "repo-b" in out.note
    assert "missing" in out.note and "re-run implement" in out.note


def test_multi_repo_failing_ci_with_clone_runs_ci_fix(tmp_path, monkeypatch):
    """A failing-CI repo WITH a workspace clone runs the CI-fix agent on that
    repo's clone, pushes the fix, and re-polls (same state) — the multi-repo
    auto-recovery the d776 follow-up wires."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
                "sha": "deadbeef",
            },
        },
        ci_responses={
            remote_b: {"conclusion": "failure", "failing": [{"name": "tests"}]},
        },
    )
    # Best-effort log enrichment must not require real network.
    monkeypatch.setattr(
        github.GitHubForge, "list_workflow_runs", lambda self, *, head_sha: []
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Materialise repo-b's clone under repos/<id> so the fix path proceeds.
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    captured = {}
    from robotsix_mill.stages import merge as merge_mod

    def fake_ci_fix(*, settings, repo_dir, branch, failing_summary, **kw):
        captured["repo_dir"] = repo_dir
        captured["branch"] = branch

        class _R:
            status = "DONE"
            updated_memory = ""

        return _R()

    monkeypatch.setattr(merge_mod, "run_ci_fix_agent", fake_ci_fix)
    # Keep the test hermetic: don't open a real Langfuse-exporting span.
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # New commits present → push path taken.
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {}
    monkeypatch.setattr(
        merge_mod.git_ops,
        "post_push_check",
        lambda repo, branch, target, remote_url, token: (
            pushed.update({"branch": branch, "remote": remote_url})
            or PostPushResult.PASS
        ),
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    # Stays in IMPLEMENT_COMPLETE to re-poll after the fix push.
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert captured["repo_dir"].endswith("repos/repo-b")
    assert pushed["remote"] == remote_b
    # Attempt counter reset on a productive push.
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b.count"
    assert merge_mod._read_counter(counter) == 0
    # The inline cross-repo loop leaves a per-attempt breadcrumb in history
    # (it never transitions to FIXING_CI, so without this the trail is empty).
    notes = [e.note or "" for e in ctx.service.history(t.id)]
    assert any(
        "ci_fix (cross-repo) attempt 1/" in n and "repo-b" in n and "tests" in n
        for n in notes
    ), notes


def test_multi_repo_ci_fix_cycle_ceiling_blocks(tmp_path, monkeypatch):
    """After ci_fix_max_cycles cycles of the agent reporting DONE + producing
    commits while CI stays red, the next call triggers BLOCKED without
    invoking the agent."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="3")
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
                "sha": "deadbeef",
            },
        },
        ci_responses={
            # check_status in _run_multi_repo returns failure (routes to ci_fix).
            # But _multi_repo_fix_ci also calls check_status — we need
            # consistent failure there too.
            remote_b: {"conclusion": "failure", "failing": [{"name": "tests"}]},
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "list_workflow_runs", lambda self, *, head_sha: []
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Materialise repo-b clone.
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    from robotsix_mill.stages import merge as merge_mod
    from robotsix_mill.agents.ci_fixing import CiFixResult

    agent_calls = {"n": 0}

    def fake_agent(**kw):
        agent_calls["n"] += 1
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr(merge_mod, "run_ci_fix_agent", fake_agent)
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # Simulate a fresh churn commit every cycle: local != remote.
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b_cycles.txt"

    # Cycles 1-3 run the agent → IMPLEMENT_COMPLETE.
    for expected in (1, 2, 3):
        out = MergeStage().run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE
        assert merge_mod._read_counter(cycle_path) == expected
    assert agent_calls["n"] == 3

    # Cycle 4 reaches the ceiling → BLOCKED without running the agent.
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "hard ceiling of 3 cycle(s)" in out.note
    # Agent NOT invoked on the blocking cycle.
    assert agent_calls["n"] == 3
    # Cycle counter reset to 0 on the blocking return.
    assert merge_mod._read_counter(cycle_path) == 0


def test_multi_repo_ci_fix_cycle_reset_on_green(tmp_path, monkeypatch):
    """Two failing cycles bump the per-repo cycle counter; then CI turns green
    → counter resets to 0."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="8")
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    # Mutable dict so we can flip the CI conclusion between calls.
    ci_state = {"conclusion": "failure", "failing": [{"name": "tests"}]}

    def fake_pr_status(self, *, source_branch):
        return {
            "merged": False,
            "state": "open",
            "url": "https://github.com/o/b/pull/2",
            "mergeable": True,
            "sha": "deadbeef",
        }

    def fake_check_status(self, *, source_branch):
        return dict(ci_state)

    monkeypatch.setattr(github.GitHubForge, "pr_status", fake_pr_status)
    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)
    monkeypatch.setattr(
        github.GitHubForge, "list_workflow_runs", lambda self, *, head_sha: []
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    from robotsix_mill.stages import merge as merge_mod
    from robotsix_mill.agents.ci_fixing import CiFixResult

    monkeypatch.setattr(
        merge_mod,
        "run_ci_fix_agent",
        lambda **kw: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    monkeypatch.setattr(
        merge_mod.git_ops, "post_push_check", lambda *a, **k: PostPushResult.PASS
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b_cycles.txt"

    # Two failing cycles bump the counter.
    MergeStage().run(t, ctx)
    MergeStage().run(t, ctx)
    assert merge_mod._read_counter(cycle_path) == 2

    # CI turns green — the _run_multi_repo poll observes success and
    # resets the counter.  With no eligible review the all-green hold now
    # surfaces HUMAN_MR_APPROVAL so a human can merge.
    ci_state["conclusion"] = "success"
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_mod._read_counter(cycle_path) == 0


def test_multi_repo_all_green_auto_merges_when_eligible(tmp_path, monkeypatch):
    """All PRs green + review marks auto-merge eligible → each green PR is
    merged via its own forge; stays same-state so the next poll sees DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "u-b",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "success", "failing": []},
        },
    )
    merged_calls = []

    def _fake_merge(self, *, source_branch):
        merged_calls.append(self._remote_url)
        return {"merged": True}

    monkeypatch.setattr(github.GitHubForge, "merge_pr", _fake_merge)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Review artifact marking auto-merge eligible.
    ctx.service.workspace(t).artifacts_dir.joinpath("review.md").write_text(
        "verdict: APPROVE\nauto_merge_eligible: true\n", encoding="utf-8"
    )
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {"repo_id": "repo-b", "branch": branch, "url": "u-b"},
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE  # re-poll → next sees DONE
    assert sorted(merged_calls) == [remote_a, remote_b]


def test_multi_repo_all_green_auto_merges_without_artifact(tmp_path, monkeypatch):
    """All PRs green — auto-merge fires without a review artifact
    (artifact check removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    remote_a = "https://github.com/o/a.git"
    _install_multirepo_registry([("repo-a", remote_a)])
    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
        },
        ci_responses={remote_a: {"conclusion": "success", "failing": []}},
    )
    merged_calls = []

    def _fake_merge(self, *, source_branch):
        merged_calls.append(1)
        return {"merged": True}

    monkeypatch.setattr(github.GitHubForge, "merge_pr", _fake_merge)
    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # No review.md — was previously not eligible, now auto-merges.
    _write_pr_urls(ctx, t, [{"repo_id": "repo-a", "branch": branch, "url": "u-a"}])

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE  # re-poll → next sees DONE
    assert merged_calls == [1]


def test_multi_repo_unknown_repo_id_blocks(tmp_path, monkeypatch):
    """A repo_id not in ReposRegistry → BLOCKED with 'unknown repo_id'."""
    ctx = _gh(tmp_path)
    _install_multirepo_registry(
        [("repo-a", "https://github.com/o/a.git")],
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "ghost-repo",
                "branch": branch,
                "url": "https://github.com/o/ghost/pull/9",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "unknown repo_id" in out.note


def test_multi_repo_corrupt_pr_urls_blocks(tmp_path):
    """Invalid JSON in pr_urls.json → BLOCKED with 'corrupted'."""
    ctx = _gh(tmp_path)
    t = _make_meta_ticket(ctx)
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "pr_urls.json").write_text("{not valid json", encoding="utf-8")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "corrupted" in out.note


def test_multi_repo_per_repo_forge_called_with_correct_remote(tmp_path, monkeypatch):
    """pr_status is invoked once per repo with that repo's _remote_url."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    seen_pr, _ = _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    MergeStage().run(t, ctx)
    assert len(seen_pr) == 2
    remotes_called = {entry["remote"] for entry in seen_pr}
    assert remotes_called == {remote_a, remote_b}


def test_single_repo_unchanged_when_no_pr_urls_json(tmp_path, monkeypatch):
    """When pr_urls.json is absent, the existing single-repo dispatch
    runs and ``merge.md`` is the byte-identical single-line shape."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://github.com/o/r/pull/77",
        },
    )

    t = _human_mr_approval(ctx)
    # Sanity: pr_urls.json must NOT exist
    assert not (ctx.service.workspace(t).artifacts_dir / "pr_urls.json").exists()
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    merge_md = (ctx.service.workspace(t).artifacts_dir / "merge.md").read_text(
        encoding="utf-8"
    )
    assert merge_md == "merged: https://github.com/o/r/pull/77\n"


def test_multi_repo_entry_missing_repo_id_blocks(tmp_path):
    """A malformed ``pr_urls.json`` entry (missing/empty/non-string
    ``repo_id``) must NOT bubble a ``KeyError`` past the caller's narrow
    ``except ConfigError`` arm — it must BLOCK cleanly.

    Pins ``_repo_config_for_entry`` raising ``ConfigError`` for the
    missing / empty / non-string-``repo_id`` cases so the aggregator's
    existing arm catches it."""
    ctx = _gh(tmp_path)
    _install_multirepo_registry([("repo-a", "https://github.com/o/a.git")])

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Entry has no ``repo_id`` key at all.
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "unknown repo_id" in out.note


# ============================================================
def test_multi_repo_fix_ci_diverged_returns_blocked_and_skips_push(
    tmp_path, monkeypatch
):
    """When reconcile reports the PR branch DIVERGED, _multi_repo_fix_ci must
    BLOCK and must NOT call post_push_check — the lease cannot protect a case
    where reconcile already fetched the foreign commit into the lease ref."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "tests"}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": ""},
    )
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # Diverged reconcile must short-circuit before any agent/push.
    monkeypatch.setattr(
        merge_mod.git_ops,
        "reconcile_with_remote_pr",
        lambda *a, **k: ReconcileResult.DIVERGED,
    )
    # If the guard were removed, the agent would run + produce a commit
    # (head != remote) → the push below would fire.
    monkeypatch.setattr(
        merge_mod,
        "run_ci_fix_agent",
        lambda **k: type("_R", (), {"status": "DONE", "updated_memory": ""})(),
    )
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {"called": False}

    def _spy_push(*a, **k):
        pushed["called"] = True
        raise AssertionError("post_push_check must not run on a diverged branch")

    monkeypatch.setattr(merge_mod.git_ops, "post_push_check", _spy_push)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    (ctx.service.workspace(t).dir / "repos" / "repo-b" / ".git").mkdir(parents=True)

    out = MergeStage()._multi_repo_fix_ci(
        t, ctx, {"repo_id": "repo-b", "branch": branch, "url": "u"}
    )
    assert out.next_state is State.BLOCKED
    assert pushed["called"] is False
    assert "diverged" in (out.note or "").lower()


def test_multi_repo_fix_ci_failed_attempt_records_history_note(tmp_path, monkeypatch):
    """A cross-repo ci-fix attempt whose agent fails must leave a per-attempt
    breadcrumb in ticket history.

    The inline multi-repo loop never transitions to FIXING_CI, so a failed
    attempt returns Outcome(IMPLEMENT_COMPLETE) with no transition row. Without
    an explicit history note, a ticket that later BLOCKs "failed after N
    attempt(s)" shows zero fixing_ci rows in /history — the mystery this fix
    closes."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "tests"}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_files", lambda self, *, source_branch: []
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": ""},
    )
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        merge_mod.git_ops, "reconcile_with_remote_pr", lambda *a, **k: None
    )
    # Agent fails (status != DONE) → failed-attempt re-poll branch.
    monkeypatch.setattr(
        merge_mod,
        "run_ci_fix_agent",
        lambda **k: type("_R", (), {"status": "ERROR", "updated_memory": ""})(),
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    (ctx.service.workspace(t).dir / "repos" / "repo-b" / ".git").mkdir(parents=True)

    out = MergeStage()._multi_repo_fix_ci(
        t, ctx, {"repo_id": "repo-b", "branch": branch, "url": "u"}
    )
    # Failed attempt re-polls (no transition) but advances the counter…
    assert out.next_state is State.IMPLEMENT_COMPLETE
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b.count"
    assert merge_mod._read_counter(counter) == 1
    # …and leaves both a start and a failure breadcrumb in history.
    notes = [e.note or "" for e in ctx.service.history(t.id)]
    assert any("ci_fix (cross-repo) attempt 1/" in n and "repo-b" in n for n in notes)
    assert any("failed (agent error)" in n for n in notes), notes


def test_multi_repo_rebase_diverged_returns_blocked_and_skips_push(
    tmp_path, monkeypatch
):
    """When reconcile reports the PR branch DIVERGED, _multi_repo_rebase must
    BLOCK and must NOT call post_push_check."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        merge_mod.git_ops,
        "reconcile_with_remote_pr",
        lambda *a, **k: ReconcileResult.DIVERGED,
    )
    monkeypatch.setattr(merge_mod.git_ops, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(
        merge_mod,
        "run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {"called": False}

    def _spy_push(*a, **k):
        pushed["called"] = True
        raise AssertionError("post_push_check must not run on a diverged branch")

    monkeypatch.setattr(merge_mod.git_ops, "post_push_check", _spy_push)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    (ctx.service.workspace(t).dir / "repos" / "repo-b" / ".git").mkdir(parents=True)

    out = MergeStage()._multi_repo_rebase(
        t, ctx, {"repo_id": "repo-b", "branch": branch, "url": "u"}
    )
    assert out.next_state is State.BLOCKED
    assert pushed["called"] is False
    assert "diverged" in (out.note or "").lower()


def test_multi_repo_one_repo_changes_requested_routes_to_addressing_review(
    tmp_path, monkeypatch
):
    """Multi-repo: all green + eligible, but one repo reports CHANGES_REQUESTED
    with comments → ADDRESSING_REVIEW; no repo's merge_pr is called."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "u-b",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "success", "failing": []},
        },
    )
    review_by_remote = {
        remote_a: None,
        remote_b: {
            "state": "CHANGES_REQUESTED",
            "comments": [{"body": "fix", "path": "b.py", "line": 2}],
            "files": ["b.py"],
        },
    }
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: review_by_remote.get(self._remote_url),
    )
    merged_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: (
            merged_calls.append(self._remote_url) or {"merged": True}
        ),
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ctx.service.workspace(t).artifacts_dir.joinpath("review.md").write_text(
        "verdict: APPROVE\nauto_merge_eligible: true\n", encoding="utf-8"
    )
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {"repo_id": "repo-b", "branch": branch, "url": "u-b"},
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.ADDRESSING_REVIEW
    assert merged_calls == []
    assert (ctx.service.workspace(t).artifacts_dir / "review_feedback.json").exists()


def test_multi_repo_no_changes_requested_auto_merges(tmp_path, monkeypatch):
    """Multi-repo: all green + eligible and no repo requests changes →
    auto-merge proceeds as today (regression guard)."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "u-b",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "success", "failing": []},
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: None,
    )
    merged_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: (
            merged_calls.append(self._remote_url) or {"merged": True}
        ),
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ctx.service.workspace(t).artifacts_dir.joinpath("review.md").write_text(
        "verdict: APPROVE\nauto_merge_eligible: true\n", encoding="utf-8"
    )
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {"repo_id": "repo-b", "branch": branch, "url": "u-b"},
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert sorted(merged_calls) == [remote_a, remote_b]
