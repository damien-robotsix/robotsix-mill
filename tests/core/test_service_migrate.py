"""Cross-board migration tests extracted from test_service.py.

Covers single-ticket migration (``migrate_ticket``) and epic-subtree
migration, including block-state reset, clone/baseline-cache pruning,
spawn-budget clearing, validation, and rollback-on-failure.

Uses the shared ``service`` / ``settings`` fixtures from the root
conftest; the ``migrate_env`` fixture below is local to these tests.
"""

import itertools

import pytest

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State

# ---------------------------------------------------------------------------
# Cross-board migration
# ---------------------------------------------------------------------------


@pytest.fixture
def migrate_env(settings, service):
    """Register two boards (test-board + other-board) in the repos
    config singleton and init the target DB, so ``migrate`` can
    validate its target. Returns (service, other_service)."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.core import db as _db

    _cfg._repos_config = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="proj-a",
                langfuse_public_key="pk-a",
                langfuse_secret_key="sk-a",
            ),
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="proj-b",
                langfuse_public_key="pk-b",
                langfuse_secret_key="sk-b",
            ),
        }
    )
    _db.init_db(settings, board_id="other-board")
    other = TicketService(settings, board_id="other-board")
    yield service, other
    _cfg._repos_config = None


def test_migrate_moves_ticket_history_and_workspace(settings, migrate_env):
    service, other = migrate_env
    t = service.create("Misrouted fix", "fix belongs to the other repo")
    service.add_comment(t.id, "a human remark")
    src_ws = settings.workspaces_dir_for("test-board") / t.id

    migrated = service.migrate(t.id, "other-board", note="belongs there")

    assert migrated.board_id == "other-board"
    assert migrated.state is State.DRAFT
    # Row moved: gone from the source DB, present in the target DB.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as TicketModel

    with _db.session(settings, "test-board") as s:
        assert s.get(TicketModel, t.id) is None
    with _db.session(settings, "other-board") as s:
        assert s.get(TicketModel, t.id) is not None

    # Workspace moved with its description.
    dst_ws = settings.workspaces_dir_for("other-board") / t.id
    assert not src_ws.exists()
    assert migrated.workspace_path == str(dst_ws)
    assert other.workspace(migrated).read_description() == (
        "fix belongs to the other repo"
    )

    # History preserved + migration event appended, hash chain intact.
    hist = other.history(t.id)
    assert hist[0].note == "created"
    assert "migrated from board 'test-board' to 'other-board'" in hist[-1].note
    assert "belongs there" in hist[-1].note
    for prev, cur in itertools.pairwise(hist):
        assert cur.prev_hash == prev.hash

    # Comments moved.
    comments = other.list_comments(t.id)
    assert [c.body for c in comments] == ["a human remark"]


def test_migrate_accepts_repo_id_and_resets_block_state(migrate_env):
    service, other = migrate_env
    t = service.create("Blocked elsewhere", "body")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="not actionable here")
    migrated = service.migrate(t.id, "other-repo")
    assert migrated.board_id == "other-board"
    assert migrated.state is State.DRAFT
    assert migrated.blocked_from is None
    assert "(was blocked)" in other.history(t.id)[-1].note


def test_migrate_prunes_repo_clone_and_baseline_cache(settings, migrate_env):
    service, _ = migrate_env
    t = service.create("With clone", "body")
    ws = service.workspace(t)
    (ws.repo_dir / ".git").mkdir(parents=True)
    (ws.artifacts_dir / "baseline_check.json").write_text("{}")
    (ws.artifacts_dir / "draft-original.md").write_text("keep me")

    service.migrate(t.id, "other-board")

    dst_ws = settings.workspaces_dir_for("other-board") / t.id
    assert not (dst_ws / "repo").exists()
    assert not (dst_ws / "artifacts" / "baseline_check.json").exists()
    assert (dst_ws / "artifacts" / "draft-original.md").read_text() == "keep me"


def test_migrate_clears_the_implement_spawn_budget(settings, migrate_env):
    """A spawn budget burned against the old repo must not follow the ticket.

    Every counted spawn was spent on the wrong repo — usually the reason
    the ticket is being migrated at all — so carrying the counter over
    re-blocks it on arrival before the new board attempts anything.
    """
    service, _ = migrate_env
    t = service.create("Exhausted elsewhere", "body")
    ws = service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("3")
    (ws.artifacts_dir / "implement_spawn_exhausted.json").write_text(
        '{"spec_fp": "deadbeef", "count": 2}'
    )

    service.migrate(t.id, "other-board")

    dst_artifacts = settings.workspaces_dir_for("other-board") / t.id / "artifacts"
    assert not (dst_artifacts / "implement_spawn_count").exists()
    assert not (dst_artifacts / "implement_spawn_exhausted.json").exists()


def test_migrate_rejects_bad_targets_and_states(migrate_env):
    service, _ = migrate_env
    t = service.create("t", "b")
    with pytest.raises(ValueError, match="unknown target board"):
        service.migrate(t.id, "no-such-board")
    with pytest.raises(ValueError, match="already on board"):
        service.migrate(t.id, "test-board")
    with pytest.raises(KeyError):
        service.migrate("nonexistent-id", "other-board")

    # Leaf epic (no children) CAN be migrated — the old hard-block is
    # lifted so that mis-filed epics can be moved.
    epic = service.create("an epic", kind=TicketKind.EPIC)
    migrated_epic = service.migrate(epic.id, "other-board")
    assert migrated_epic.board_id == "other-board"
    assert migrated_epic.state is State.DRAFT

    # A non-epic child linked to a parent KEEPS the parent link across the
    # migration — cross-board parent links are now supported, so the link
    # survives the move intact.
    parent = service.create("parent task")
    child = service.create("child task", parent_id=parent.id)
    migrated_child = service.migrate(child.id, "other-board")
    assert migrated_child.board_id == "other-board"
    assert migrated_child.parent_id == parent.id

    # A non-epic parent WITH children is still blocked (the subtree
    # path only triggers for kind == TicketKind.EPIC).
    parent2 = service.create("parent2 task")
    service.create("child2 task", parent_id=parent2.id)
    with pytest.raises(ValueError, match="has child tickets"):
        service.migrate(parent2.id, "other-board")

    # In-flight states refuse migration.
    busy = service.create("busy", "b")
    service.transition(busy.id, State.READY)
    service.transition(busy.id, State.DELIVERABLE)
    with pytest.raises(ValueError, match="can be migrated"):
        service.migrate(busy.id, "other-board")


# ---------------------------------------------------------------------------
# Epic subtree migration
# ---------------------------------------------------------------------------


def test_migrate_epic_subtree_moves_all_tickets(settings, migrate_env):
    """An epic with children and grandchildren migrates atomically:
    all tickets land on the target board with parent_id links intact,
    and none remain on the source board."""
    service, other = migrate_env

    # Build a 3-level tree: epic → child_a → grandchild
    #                           epic → child_b
    epic = service.create("Epic", kind=TicketKind.EPIC)
    child_a = service.create("Child A", parent_id=epic.id)
    child_b = service.create("Child B", parent_id=epic.id)
    grandchild = service.create("Grandchild", parent_id=child_a.id)

    # Add comments and history to some tickets so we verify preservation.
    service.add_comment(epic.id, "epic comment")
    service.add_comment(child_a.id, "child comment")

    # Workspace dirs — create marker files so we can verify the move.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        ws = settings.workspaces_dir_for("test-board") / tid
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "description.md").write_text(f"desc-{tid}")

    migrated = service.migrate(epic.id, "other-board", note="wrong board")

    # Root ticket returned.
    assert migrated.id == epic.id
    assert migrated.board_id == "other-board"
    assert migrated.state is State.DRAFT

    # All four tickets exist on the target board.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        t = other.get(tid)
        assert t is not None, f"{tid} missing from target board"
        assert t.board_id == "other-board"
        assert t.state is State.DRAFT

    # Parent links intact.
    assert other.get(child_a.id).parent_id == epic.id
    assert other.get(child_b.id).parent_id == epic.id
    assert other.get(grandchild.id).parent_id == child_a.id

    # All four tickets gone from source board.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as TicketModel

    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        with _db.session(settings, "test-board") as s:
            assert s.get(TicketModel, tid) is None, f"{tid} still on source board"

    # Workspace dirs moved to target board.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        src_ws = settings.workspaces_dir_for("test-board") / tid
        dst_ws = settings.workspaces_dir_for("other-board") / tid
        assert not src_ws.exists(), f"source workspace {tid} still exists"
        assert dst_ws.exists(), f"target workspace {tid} missing"
        assert (dst_ws / "description.md").read_text() == f"desc-{tid}"

    # History preserved: each ticket has at least a creation event and
    # a migration event.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        hist = other.history(tid)
        assert len(hist) >= 2, f"{tid}: expected ≥2 events, got {len(hist)}"
        assert hist[0].note == "created"
        assert "migrated from board 'test-board' to 'other-board'" in hist[-1].note

    # Comments preserved.
    assert [c.body for c in other.list_comments(epic.id)] == ["epic comment"]
    assert [c.body for c in other.list_comments(child_a.id)] == ["child comment"]
    assert other.list_comments(child_b.id) == []
    assert other.list_comments(grandchild.id) == []

    # Hash chain intact for each ticket.
    for tid in [epic.id, child_a.id, child_b.id, grandchild.id]:
        hist = other.history(tid)
        for prev, cur in itertools.pairwise(hist):
            assert cur.prev_hash == prev.hash, f"{tid}: broken hash chain"


def test_migrate_epic_subtree_rejects_non_migratable_child(migrate_env):
    """A child in a non-migratable state blocks the entire subtree
    migration with a clear error naming the offending ticket."""
    service, _ = migrate_env

    epic = service.create("Epic", kind=TicketKind.EPIC)
    child_ok = service.create("OK child", parent_id=epic.id)
    child_bad = service.create("Bad child", parent_id=epic.id)
    # Transition child_bad to DELIVERABLE — not in _MIGRATABLE_STATES.
    service.transition(child_bad.id, State.READY)
    service.transition(child_bad.id, State.DELIVERABLE)

    with pytest.raises(ValueError, match="non-migratable states"):
        service.migrate(epic.id, "other-board")

    # The error should name the blocking child.
    with pytest.raises(ValueError, match=child_bad.id):
        service.migrate(epic.id, "other-board")

    # Verify nothing moved: the epic and children still on source board.
    assert service.get(epic.id) is not None
    assert service.get(child_ok.id) is not None
    assert service.get(child_bad.id) is not None


def test_migrate_epic_subtree_rolls_back_on_db_failure(settings, migrate_env):
    """When the target DB insert fails mid-subtree, workspace dirs are
    rolled back to the source board and the source DB is untouched."""
    service, _other = migrate_env

    epic = service.create("Epic", kind=TicketKind.EPIC)
    child = service.create("Child", parent_id=epic.id)

    # Create workspace dirs with marker files.
    for tid in [epic.id, child.id]:
        ws = settings.workspaces_dir_for("test-board") / tid
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "description.md").write_text(f"desc-{tid}")

    # Pre-create a ticket with the same ID as the child on the target
    # board, so the insert of the child row will fail with an integrity
    # error — triggering the rollback path.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as TicketModel

    with _db.session(settings, "other-board") as s:
        s.add(
            TicketModel(
                id=child.id,
                title="collision",
                board_id="other-board",
                workspace_path=str(
                    settings.workspaces_dir_for("other-board") / child.id
                ),
            )
        )
        s.commit()

    import sqlalchemy.exc

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        service.migrate(epic.id, "other-board")

    # Workspace dirs rolled back: source still has them, target does not.
    for tid in [epic.id, child.id]:
        src_ws = settings.workspaces_dir_for("test-board") / tid
        dst_ws = settings.workspaces_dir_for("other-board") / tid
        assert src_ws.exists(), f"source workspace {tid} was not rolled back"
        assert (src_ws / "description.md").read_text() == f"desc-{tid}", (
            f"source workspace {tid} content lost"
        )
        # Target dirs should not exist (or be empty if created then rolled back).
        assert not dst_ws.exists() or not any(dst_ws.iterdir()), (
            f"target workspace {tid} not cleaned up"
        )

    # Source DB untouched: both tickets still present.
    assert service.get(epic.id) is not None
    assert service.get(child.id) is not None
