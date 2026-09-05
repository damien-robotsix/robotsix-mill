import json

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings, repos_registry):
    # TestClient runs the lifespan: init_db, worker start/stop.
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


@pytest.fixture
def multi_repo_client(settings, two_repo_registry):
    # Multi-repo mode: no single_repo_id, so /repos surfaces every
    # registered repo plus the synthetic "meta" entry.
    with TestClient(create_app(two_repo_registry, settings)) as c:
        yield c


@pytest.fixture
def clean_failures():
    """Keep the module-global Langfuse failure registry clean so test
    ordering can't bleed across langfuse-status tests."""
    from robotsix_mill.runtime.tracing import clear_export_failures

    clear_export_failures()
    yield
    clear_export_failures()


# ---------------------------------------------------------------------------
# Cumulative cost tests
# ---------------------------------------------------------------------------


def test_epic_detail_cost_is_cumulative(client, service, monkeypatch):
    """GET /tickets/{epic_id} returns cost_usd = epic's own session cost,
    and cumulative_cost = epic own cost + all children."""
    epic = service.create("Epic", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", kind=TicketKind.TASK, parent_id=epic.id)
    c2 = service.create("Child 2", kind=TicketKind.TASK, parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: {
            epic.id: 0.01,
            c1.id: 0.10,
            c2.id: 0.20,
        }.get(sid, 0.0),
    )

    r = client.get(f"/tickets/{epic.id}").json()
    assert r["cost_usd"] == pytest.approx(0.01)  # epic's own session cost
    assert r["cumulative_cost"] == pytest.approx(0.31)  # 0.01 + 0.10 + 0.20


def test_epic_list_cost_is_cache_only(client, service, monkeypatch):
    """GET /tickets builds its RESPONSE cache-only — epic cumulative cost must
    not trigger blocking session_cost during the request. (Children are still
    warmed afterwards by the background task, which is fine.)"""
    epic = service.create("Epic", kind=TicketKind.EPIC)
    service.create("Child 1", kind=TicketKind.TASK, parent_id=epic.id)
    service.create("Child 2", kind=TicketKind.TASK, parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: 0.999,
    )

    ts = client.get("/tickets").json()
    epic_entry = [x for x in ts if x["id"] == epic.id]
    assert len(epic_entry) == 1
    # RESPONSE is cache-only: epic's own cost 0.0 and cumulative not computed,
    # even though session_cost returns 0.999.
    assert epic_entry[0]["cost_usd"] == 0.0
    assert epic_entry[0]["cumulative_cost"] is None


def test_nested_epic_cost_is_recursive(client, service, monkeypatch):
    """Epic → sub-epic → task: top epic cumulative includes all three,
    but cost_usd stays as its own direct session cost."""
    e1 = service.create("E1", kind=TicketKind.EPIC)
    e2 = service.create("E2", kind=TicketKind.EPIC, parent_id=e1.id)
    t = service.create("T", kind=TicketKind.TASK, parent_id=e2.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: {e1.id: 0.01, e2.id: 0.02, t.id: 0.30}.get(
            sid, 0.0
        ),
    )

    r1 = client.get(f"/tickets/{e1.id}").json()
    assert r1["cost_usd"] == pytest.approx(0.01)  # e1's own session cost
    assert r1["cumulative_cost"] == pytest.approx(0.33)  # 0.01 + 0.02 + 0.30

    r2 = client.get(f"/tickets/{e2.id}").json()
    assert r2["cost_usd"] == pytest.approx(0.02)  # e2's own session cost
    assert r2["cumulative_cost"] == pytest.approx(0.32)  # 0.02 + 0.30


def test_ticket_with_children_has_cumulative_cost(client, service, monkeypatch):
    """A non-epic ticket with child tickets gets cumulative_cost > cost_usd."""
    parent = service.create("Parent task", kind=TicketKind.TASK)
    c1 = service.create("Child 1", kind=TicketKind.TASK, parent_id=parent.id)
    c2 = service.create("Child 2", kind=TicketKind.TASK, parent_id=parent.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: {
            parent.id: 0.05,
            c1.id: 0.10,
            c2.id: 0.07,
        }.get(sid, 0.0),
    )

    r = client.get(f"/tickets/{parent.id}").json()
    assert r["cost_usd"] == pytest.approx(0.05)
    assert r["cumulative_cost"] == pytest.approx(0.22)  # 0.05 + 0.10 + 0.07


def test_leaf_ticket_cumulative_cost_is_none(client, service, monkeypatch):
    """A ticket with no children has cumulative_cost: null in JSON."""
    leaf = service.create("Leaf task", kind=TicketKind.TASK)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: 0.042 if sid == leaf.id else 0.0,
    )

    r = client.get(f"/tickets/{leaf.id}").json()
    assert r["cost_usd"] == pytest.approx(0.042)
    assert r["cumulative_cost"] is None


def test_board_js_references_cumulative_cost(client):
    """board-mill.js contains references to cumulative_cost for the split
    badge and drawer rendering."""
    js = client.get("/static/mill/board-mill.js").text
    assert "cumulative_cost" in js


# ---------------------------------------------------------------------------
# merge-now / merge-reason tests
# ---------------------------------------------------------------------------


class _FakeForge:
    """Minimal forge stub for merge-now / update-branch endpoint tests."""

    _UNSET = object()

    def __init__(
        self,
        merge_result=_UNSET,
        pr_status_result=_UNSET,
        update_branch_result=_UNSET,
    ):
        if merge_result is self._UNSET:
            merge_result = {"merged": True, "reason": "merged"}
        if pr_status_result is self._UNSET:
            pr_status_result = {
                "url": "https://github.com/test/pr/1",
                "merged": False,
                "state": "open",
                "mergeable": True,
            }
        if update_branch_result is self._UNSET:
            update_branch_result = {"updated": True, "reason": "update-branch accepted"}
        self._merge_result = merge_result
        self._pr_status_result = pr_status_result
        self._update_branch_result = update_branch_result
        self.merge_calls: list[dict] = []
        self.pr_status_calls: list[dict] = []
        self.update_branch_calls: list[dict] = []

    def merge_pr(self, *, source_branch: str) -> dict:
        self.merge_calls.append({"source_branch": source_branch})
        return self._merge_result

    def pr_status(self, *, source_branch: str) -> dict | None:
        self.pr_status_calls.append({"source_branch": source_branch})
        return self._pr_status_result

    def update_branch(self, *, source_branch: str) -> dict:
        self.update_branch_calls.append({"source_branch": source_branch})
        return self._update_branch_result


def _patch_forge(monkeypatch, fake_forge):
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge.get_forge",
        lambda s, repo_config=None: fake_forge,
    )


def _install_meta_registry(monkeypatch, repo_id, remote_url):
    """Point the global repo registry at a single meta-board repo so
    ``_repo_config_for_entry`` resolves it."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.config import RepoConfig, ReposRegistry

    monkeypatch.setattr(
        _cfg,
        "_repos_config",
        ReposRegistry(
            repos={
                repo_id: RepoConfig(
                    repo_id=repo_id,
                    board_id="meta",
                    langfuse_project_name=f"p-{repo_id}",
                    langfuse_public_key=f"pk-{repo_id}",
                    langfuse_secret_key=f"sk-{repo_id}",
                    forge_remote_url=remote_url,
                )
            }
        ),
    )


class _RecordedUrlForge:
    """Records pr_status_by_url calls; resolves the recorded URL and
    reports open+mergeable with green CI, unless told to miss."""

    def __init__(self, *, resolved=True, merged=False):
        self.by_url_calls: list[str] = []
        self.branch_calls: list[str] = []
        self._resolved = resolved
        self._merged = merged

    def pr_status_by_url(self, *, url: str):
        self.by_url_calls.append(url)
        if not self._resolved:
            return None
        return {
            "url": url,
            "merged": self._merged,
            "state": "closed" if self._merged else "open",
            "mergeable": True,
        }

    def pr_status(self, *, source_branch: str):
        # Must never be reached on the recorded-URL path — the bug this
        # guards: the board-derived branch-keyed lookup.
        self.branch_calls.append(source_branch)

    def check_status(self, *, source_branch: str, require_checks=False):
        return {"conclusion": "success", "failing": []}


def _meta_ticket(service, title="Meta ticket"):
    """Create a meta-board ticket in IMPLEMENT_COMPLETE with a branch."""
    t = service.create(title)
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t.id, st, note=f"-> {st.value}")
    service.set_branch(t.id, "mill/meta")
    return service.get(t.id)


def test_merge_status_multi_repo_resolves_recorded_url(client, service, monkeypatch):
    """GET /tickets/{id}/merge-status for a meta ticket resolves the PRs
    by their recorded ``pr_urls.json`` URLs in each repo's own forge —
    never by a branch-keyed lookup against the board-derived repo."""
    remote = "https://github.com/o/meta-a.git"
    recorded_url = "https://github.com/o/meta-a/pull/9"
    _install_meta_registry(monkeypatch, "meta-a", remote)
    fake = _RecordedUrlForge()
    _patch_forge(monkeypatch, fake)

    t = _meta_ticket(service)
    ws = service.workspace(t)
    (ws.artifacts_dir / "pr_urls.json").write_text(
        json.dumps([{"repo_id": "meta-a", "branch": "mill/meta", "url": recorded_url}]),
        encoding="utf-8",
    )

    r = client.get(f"/tickets/{t.id}/merge-status")
    assert r.status_code == 200
    data = r.json()
    assert data["can_merge"] is True
    assert data["ci_conclusion"] == "success"
    assert fake.by_url_calls == [recorded_url]
    assert fake.branch_calls == []  # recorded URL is authoritative


def test_merge_status_multi_repo_unresolved_reports_recorded_url(
    client, service, monkeypatch
):
    """When the recorded URL does not resolve, can_merge is False with a
    reason naming the URL problem — not the board repo's branch."""
    remote = "https://github.com/o/meta-a.git"
    recorded_url = "https://github.com/o/meta-a/pull/9"
    _install_meta_registry(monkeypatch, "meta-a", remote)
    fake = _RecordedUrlForge(resolved=False)
    _patch_forge(monkeypatch, fake)

    t = _meta_ticket(service)
    ws = service.workspace(t)
    (ws.artifacts_dir / "pr_urls.json").write_text(
        json.dumps([{"repo_id": "meta-a", "branch": "mill/meta", "url": recorded_url}]),
        encoding="utf-8",
    )

    r = client.get(f"/tickets/{t.id}/merge-status")
    assert r.status_code == 200
    data = r.json()
    assert data["can_merge"] is False
    assert "no PR found for the recorded URL" in data["reason"]
    assert "meta-a" in data["reason"]


def test_merge_now_happy_path(client, service, monkeypatch):
    """POST /tickets/{id}/merge-now on human_mr_approval merges and
    transitions to done."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = service.create("Merge me please")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    data = r.json()
    assert data["id"] == t.id
    assert data["state"] == "done"

    # Verify the forge was called with the right branch.
    assert len(fake.merge_calls) == 1
    assert fake.merge_calls[0]["source_branch"] == t.branch

    # Verify the history contains the merge note.
    history = service.history(t.id)
    notes = " ".join(e.note or "" for e in history)
    assert "merged via board" in notes


def test_merge_now_squash_uses_merge_commit_sha(client, service, monkeypatch):
    """merge-now uses the merge commit SHA (not the branch head SHA) for
    verification.  Under a squash merge the branch head is never on main,
    so using it would incorrectly reject a successful merge."""
    branch_head_sha = "aabbccdd" * 5  # 40-char fake SHA
    merge_commit_sha = "11223344" * 5  # different 40-char fake SHA
    captured_shas: list[str] = []

    def fake_verify(repo_dir, sha, ticket_id, target="main"):
        captured_shas.append(sha)
        return True

    fake = _FakeForge(
        merge_result={
            "merged": True,
            "reason": "merged",
            "merge_commit_sha": merge_commit_sha,
        },
        pr_status_result={
            "url": "https://github.com/test/pr/1",
            "merged": True,
            "state": "closed",
            "sha": branch_head_sha,
        },
    )
    _patch_forge(monkeypatch, fake)
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge._verify_merge_ancestor",
        fake_verify,
    )

    t = _to_human_mr_approval(service, "Squash merge")
    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    assert service.get(t.id).state is State.DONE

    # The verification must receive the merge commit SHA, not the branch head.
    assert len(captured_shas) == 1
    assert captured_shas[0] == merge_commit_sha


def test_merge_now_blocks_when_not_merged_to_mainline(client, service, monkeypatch):
    """merge-now refuses the DONE transition when the merged commit is
    not an ancestor of the target branch (forge reported success but the
    work never reached mainline)."""
    fake = _FakeForge(merge_result={"merged": True, "reason": "merged"})
    _patch_forge(monkeypatch, fake)
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge._verify_merge_ancestor",
        lambda *a, **k: False,
    )

    t = _to_human_mr_approval(service, "Diverged merge")
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409, f"Got {r.status_code}: {r.text}"

    # Ticket stays parked; no DONE transition, no merge note.
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "merged via board" not in notes


def test_merge_now_multi_repo_blocks_when_not_merged_to_mainline(
    client, service, monkeypatch
):
    """Multi-repo merge-now refuses the DONE transition when a merged
    commit is not an ancestor of its repo's target branch."""
    forge_a = _FakeForge()
    forge_b = _FakeForge()
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge._verify_merge_ancestor",
        lambda *a, **k: False,
    )

    t = _to_human_mr_approval(service, "Multi-repo diverged")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409, f"Got {r.status_code}: {r.text}"
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "merged via board" not in notes


def test_merge_now_wrong_state_409(client, service, monkeypatch):
    """POST /tickets/{id}/merge-now on a non-human_mr_approval ticket
    returns 409."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = service.create("Ready ticket")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    assert service.get(t.id).state is State.READY

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409
    assert "not in human_mr_approval" in r.text.lower()

    # Forge should never have been called.
    assert len(fake.merge_calls) == 0


def test_merge_now_missing_ticket_404(client, monkeypatch):
    """POST /tickets/{id}/merge-now with a bogus id returns 404."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)
    r = client.post("/tickets/nonexistent/merge-now")
    assert r.status_code == 404


def test_merge_now_forge_rejection_409(client, service, monkeypatch):
    """POST /tickets/{id}/merge-now when the forge rejects returns 409
    and leaves the ticket state unchanged."""
    fake = _FakeForge(
        merge_result={"merged": False, "reason": "branch protection rules"},
    )
    _patch_forge(monkeypatch, fake)

    t = service.create("Blocked merge")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409
    assert "branch protection rules" in r.text

    # Ticket state must be unchanged.
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL


def _to_human_mr_approval(service, title):
    """Walk a fresh ticket up to HUMAN_MR_APPROVAL for merge-now tests."""
    t = service.create(title)
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    return service.get(t.id)


def _write_pr_urls(service, ticket, entries):
    """Write a ``pr_urls.json`` manifest into the ticket's artifacts dir."""
    d = service.workspace(ticket).artifacts_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "pr_urls.json").write_text(json.dumps(entries), encoding="utf-8")


def _patch_multirepo_forge(monkeypatch, forges_by_repo):
    """Route per-repo ``get_forge`` calls to per-repo fakes.

    ``_repo_config_for_entry`` is stubbed to return a tiny RepoConfig-like
    stand-in carrying the entry's ``repo_id`` (which keys the per-repo
    forge in the patched ``get_forge``) and an empty ``working_branch`` so
    ``target_branch_for`` falls back to the default target branch.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(
        "robotsix_mill.stages.merge._repo_config_for_entry",
        lambda entry: SimpleNamespace(repo_id=entry["repo_id"], working_branch=""),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge.get_forge",
        lambda s, repo_config=None: forges_by_repo[repo_config.repo_id],
    )


def test_merge_now_multi_repo_merges_every_repo(client, service, monkeypatch):
    """merge-now on a multi-repo ticket merges every repo's PR (one
    merge_pr per repo via its own forge) and transitions to done."""
    forge_a = _FakeForge()
    forge_b = _FakeForge()
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})

    t = _to_human_mr_approval(service, "Multi-repo merge")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    assert r.json()["state"] == "done"

    # One merge per repo, each on its own per-repo branch.
    assert [c["source_branch"] for c in forge_a.merge_calls] == ["mill/a"]
    assert [c["source_branch"] for c in forge_b.merge_calls] == ["mill/b"]


def test_merge_now_multi_repo_one_rejected_409(client, service, monkeypatch):
    """When one repo's merge is rejected, merge-now returns 409 naming
    that repo and leaves the ticket in human_mr_approval."""
    forge_a = _FakeForge()
    forge_b = _FakeForge(merge_result={"merged": False, "reason": "branch protection"})
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})

    t = _to_human_mr_approval(service, "Multi-repo reject")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409
    assert "repo-b" in r.text
    assert "branch protection" in r.text

    # repo-a stays merged (skipped on retry); ticket state unchanged.
    assert len(forge_a.merge_calls) == 1
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL


def test_merge_now_multi_repo_skips_already_merged(client, service, monkeypatch):
    """An already-merged repo is skipped (idempotent re-press); the
    remaining repo is merged and the ticket reaches done."""
    forge_a = _FakeForge(
        pr_status_result={"url": "u-a", "merged": True, "state": "closed"},
    )
    forge_b = _FakeForge()
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})

    t = _to_human_mr_approval(service, "Multi-repo idempotent")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    assert r.json()["state"] == "done"

    # repo-a already merged → skipped; repo-b merged.
    assert forge_a.merge_calls == []
    assert [c["source_branch"] for c in forge_b.merge_calls] == ["mill/b"]


def test_merge_reason_returns_file(client, service):
    """GET /tickets/{id}/merge-reason returns the contents of
    merge_reason.txt from the workspace."""
    t = service.create("Reason ticket")
    reason_path = service.workspace(t).artifacts_dir / "merge_reason.txt"
    reason_path.parent.mkdir(parents=True, exist_ok=True)
    reason_path.write_text("auto-merge disabled in config", encoding="utf-8")

    r = client.get(f"/tickets/{t.id}/merge-reason")
    assert r.status_code == 200
    assert r.json() == {"reason": "auto-merge disabled in config"}


def test_merge_reason_empty_when_no_file(client, service):
    """GET /tickets/{id}/merge-reason returns an empty reason when the
    file doesn't exist."""
    t = service.create("No reason file ticket")

    r = client.get(f"/tickets/{t.id}/merge-reason")
    assert r.status_code == 200
    assert r.json() == {"reason": ""}


def test_merge_reason_missing_ticket_404(client):
    """GET /tickets/{id}/merge-reason with a bogus id returns 404."""
    r = client.get("/tickets/nonexistent/merge-reason")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# update-branch tests
# ---------------------------------------------------------------------------


def _to_merge_relevant_state(service, title, target_state):
    """Walk a fresh ticket to a merge-relevant state for update-branch tests."""
    t = service.create(title)
    service.set_branch(t.id, f"mill/{t.id}")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    if target_state is State.WAITING_AUTO_MERGE:
        service.transition(t.id, State.WAITING_AUTO_MERGE, note="auto-merge polling")
    elif target_state is State.HUMAN_MR_APPROVAL:
        service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    # IMPLEMENT_COMPLETE is already reached above.
    return service.get(t.id)


def test_update_branch_human_mr_approval(client, service, monkeypatch):
    """POST /tickets/{id}/update-branch on human_mr_approval calls
    forge.update_branch and returns the result dict."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = _to_merge_relevant_state(service, "Stale PR", State.HUMAN_MR_APPROVAL)
    r = client.post(f"/tickets/{t.id}/update-branch")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    assert r.json() == {"updated": True, "reason": "update-branch accepted"}

    assert len(fake.update_branch_calls) == 1
    assert fake.update_branch_calls[0]["source_branch"] == t.branch


def test_update_branch_waiting_auto_merge(client, service, monkeypatch):
    """POST /tickets/{id}/update-branch on waiting_auto_merge works."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = _to_merge_relevant_state(service, "Stale WAM", State.WAITING_AUTO_MERGE)
    r = client.post(f"/tickets/{t.id}/update-branch")
    assert r.status_code == 200
    assert r.json()["updated"] is True


def test_update_branch_implement_complete(client, service, monkeypatch):
    """POST /tickets/{id}/update-branch on implement_complete works."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = _to_merge_relevant_state(service, "Stale IC", State.IMPLEMENT_COMPLETE)
    r = client.post(f"/tickets/{t.id}/update-branch")
    assert r.status_code == 200
    assert r.json()["updated"] is True


def test_update_branch_already_up_to_date(client, service, monkeypatch):
    """POST /tickets/{id}/update-branch returns updated=false when the
    branch is already current."""
    fake = _FakeForge(
        update_branch_result={"updated": False, "reason": "already up to date"},
    )
    _patch_forge(monkeypatch, fake)

    t = _to_merge_relevant_state(service, "Current PR", State.HUMAN_MR_APPROVAL)
    r = client.post(f"/tickets/{t.id}/update-branch")
    assert r.status_code == 200
    assert r.json() == {"updated": False, "reason": "already up to date"}


def test_update_branch_wrong_state_409(client, service, monkeypatch):
    """POST /tickets/{id}/update-branch on a non-merge-relevant state
    returns 409."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = service.create("Ready ticket")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    assert service.get(t.id).state is State.READY

    r = client.post(f"/tickets/{t.id}/update-branch")
    assert r.status_code == 409
    assert "not in a merge-relevant state" in r.text.lower()


def test_update_branch_missing_ticket_404(client, monkeypatch):
    """POST /tickets/{id}/update-branch with a bogus id returns 404."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)
    r = client.post("/tickets/nonexistent/update-branch")
    assert r.status_code == 404


def test_update_branch_no_branch_400(client, service, monkeypatch):
    """POST /tickets/{id}/update-branch when the ticket has no branch
    returns 400."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = service.create("No branch ticket")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    # Branch was never set — service.create leaves it None.
    ticket = service.get(t.id)
    assert ticket.state is State.HUMAN_MR_APPROVAL
    assert ticket.branch is None

    r = client.post(f"/tickets/{t.id}/update-branch")
    assert r.status_code == 400


def test_update_branch_done_state_409(client, service, monkeypatch):
    """POST /tickets/{id}/update-branch on done returns 409."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = service.create("Done ticket")
    service.transition(t.id, State.DONE, note="merged")
    assert service.get(t.id).state is State.DONE

    r = client.post(f"/tickets/{t.id}/update-branch")
    assert r.status_code == 409
