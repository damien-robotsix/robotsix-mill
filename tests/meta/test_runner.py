"""Tests for ``run_meta_pass`` in ``robotsix_mill.meta.runner``."""

from __future__ import annotations

import logging
from pathlib import Path

from robotsix_mill.meta.agent import DraftProposal, MetaAgentResult
from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.meta.runner import run_meta_pass, MetaPassResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_cfg(
    repo_id: str,
    *,
    board_id: str | None = None,
    forge_remote_url: str | None = None,
) -> RepoConfig:
    return RepoConfig(
        repo_id=repo_id,
        board_id=board_id or repo_id,
        langfuse_project_name=f"proj-{repo_id}",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        forge_remote_url=forge_remote_url,
    )


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointed at tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Settings(**overrides)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunMetaPass:
    def test_no_repos_configured_empty_result(self, tmp_path, monkeypatch):
        """When clone_all_repos returns {} the agent runs and yields
        zero drafts.  Memory is persisted (even if empty)."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        # --- mocks ---
        # Ensure run_meta_pass uses OUR settings, not a fresh Settings()
        # which would pick up the patched YAML data_dir.
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {},
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.run_meta_agent",
            lambda **kw: MetaAgentResult(updated_memory=""),
        )

        persist_calls: list[tuple[Path, str]] = []

        def _fake_persist(path, text):
            persist_calls.append((path, text))

        monkeypatch.setattr("robotsix_mill.meta.runner.persist_memory", _fake_persist)
        monkeypatch.setattr("robotsix_mill.meta.runner.load_memory", lambda _p: "")
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "<recent_proposals>\n(no recent proposals)\n</recent_proposals>",
        )

        # --- run ---
        result = run_meta_pass("test-session")

        assert isinstance(result, MetaPassResult)
        assert result.updated_memory == ""
        assert result.extraction_drafts_created == []
        assert result.alignment_drafts_created == []
        assert result.session_id == "test-session"

        # persist_memory was called with the meta memory file path
        assert len(persist_calls) == 1
        path, text = persist_calls[0]
        assert path.name == "meta_memory.md"
        assert text == ""

        db.reset_engine()

    def test_successful_pass_extraction_and_alignment(self, tmp_path, monkeypatch):
        """Extraction drafts → meta board; alignment drafts →
        per-repo board.  Both carry source=META and gap-id markers."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")
        db.init_db(settings, board_id="repo-a")
        db.init_db(settings, board_id="repo-b")

        # Ensure run_meta_pass uses OUR settings
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )

        clone_path = tmp_path / "clones"
        repo_a_path = clone_path / "repo-a"
        repo_b_path = clone_path / "repo-b"
        repo_a_path.mkdir(parents=True)
        repo_b_path.mkdir(parents=True)

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {"repo-a": repo_a_path, "repo-b": repo_b_path},
        )

        # Agent returns one extraction + one alignment draft
        agent_result = MetaAgentResult(
            updated_memory="meta ledger v1",
            extraction_drafts=[
                DraftProposal(
                    title="Extract shared util",
                    body="We should extract foo.",
                    target_repo_id=None,
                ),
            ],
            alignment_drafts=[
                DraftProposal(
                    title="Adopt pattern from repo-a",
                    body="Repo-b should adopt pattern X.",
                    target_repo_id="repo-b",
                ),
            ],
            todo_drafts=[
                DraftProposal(
                    title="Resolve TODO in repo-a",
                    body="TODO: refactor foo() in repo-a/src/foo.py.",
                    target_repo_id="repo-a",
                ),
            ],
        )

        agent_kwargs: list[dict] = []

        def _fake_agent(**kw):
            agent_kwargs.append(kw)
            return agent_result

        monkeypatch.setattr("robotsix_mill.meta.runner.run_meta_agent", _fake_agent)

        # Repos config
        reg = ReposRegistry(
            repos={
                "repo-a": _repo_cfg("repo-a", board_id="repo-a"),
                "repo-b": _repo_cfg("repo-b", board_id="repo-b"),
            }
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.get_repos_config", lambda: reg)

        # Capture persist_memory
        persist_calls: list[tuple[Path, str]] = []

        def _fake_persist(path, text):
            persist_calls.append((path, text))

        monkeypatch.setattr("robotsix_mill.meta.runner.persist_memory", _fake_persist)
        monkeypatch.setattr("robotsix_mill.meta.runner.load_memory", lambda _p: "")
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "<recent_proposals>\n(no recent proposals)\n</recent_proposals>",
        )

        # --- run ---
        result = run_meta_pass("test-session")

        # Agent was invoked with correct args
        assert len(agent_kwargs) == 1
        assert agent_kwargs[0]["repo_clones"] == {
            "repo-a": repo_a_path,
            "repo-b": repo_b_path,
        }
        assert agent_kwargs[0]["memory"] == ""

        # Result structure
        assert result.updated_memory == "meta ledger v1"
        assert len(result.extraction_drafts_created) == 1
        assert len(result.alignment_drafts_created) == 1
        assert len(result.todo_drafts_created) == 1

        # Extraction draft on meta board
        meta_svc = TicketService(settings, board_id="meta")
        meta_tickets = meta_svc.list()
        assert len(meta_tickets) == 1
        ext_ticket = meta_tickets[0]
        assert ext_ticket.title == "Extract shared util"
        assert ext_ticket.source == "meta"
        assert ext_ticket.origin_session == "test-session"
        ext_desc = Workspace(
            settings.workspaces_dir_for("meta"), ext_ticket.id
        ).read_description()
        assert "<!-- meta-gap-id: extract-shared-util -->" in ext_desc

        # Alignment draft on repo-b board
        repo_b_svc = TicketService(settings, board_id="repo-b")
        repo_b_tickets = repo_b_svc.list()
        assert len(repo_b_tickets) == 1
        aln_ticket = repo_b_tickets[0]
        assert aln_ticket.title == "Adopt pattern from repo-a"
        assert aln_ticket.source == "meta"
        assert aln_ticket.origin_session == "test-session"
        aln_desc = Workspace(
            settings.workspaces_dir_for("repo-b"), aln_ticket.id
        ).read_description()
        assert "<!-- meta-gap-id: adopt-pattern-from-repo-a -->" in aln_desc

        # TODO draft on repo-a board
        repo_a_svc = TicketService(settings, board_id="repo-a")
        repo_a_tickets = repo_a_svc.list()
        assert len(repo_a_tickets) == 1
        todo_ticket = repo_a_tickets[0]
        assert todo_ticket.title == "Resolve TODO in repo-a"
        assert todo_ticket.source == "meta"
        assert todo_ticket.origin_session == "test-session"
        todo_desc = Workspace(
            settings.workspaces_dir_for("repo-a"), todo_ticket.id
        ).read_description()
        assert "<!-- meta-gap-id: resolve-todo-in-repo-a -->" in todo_desc

        # persist_memory called with correct path
        assert len(persist_calls) == 1
        path, text = persist_calls[0]
        assert path.name == "meta_memory.md"
        assert text == "meta ledger v1"

        db.reset_engine()

    def test_alignment_draft_unknown_target_repo_id(
        self, tmp_path, monkeypatch, caplog
    ):
        """An alignment draft targeting a non-existent repo_id is
        skipped with a warning; other drafts are still filed."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")
        db.init_db(settings, board_id="repo-a")

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {},
        )

        agent_result = MetaAgentResult(
            updated_memory="ledger",
            extraction_drafts=[
                DraftProposal(
                    title="Extract X",
                    body="body",
                    target_repo_id=None,
                ),
            ],
            alignment_drafts=[
                DraftProposal(
                    title="Bad alignment",
                    body="bad body",
                    target_repo_id="no-such-repo",
                ),
            ],
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.run_meta_agent",
            lambda **kw: agent_result,
        )

        reg = ReposRegistry(
            repos={
                "repo-a": _repo_cfg("repo-a", board_id="repo-a"),
            }
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.get_repos_config", lambda: reg)

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.persist_memory", lambda p, t: None
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.load_memory", lambda _p: "")
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "",
        )

        with caplog.at_level(logging.WARNING):
            result = run_meta_pass("test-session")

        # Warning logged about unknown repo
        assert any("no-such-repo" in m and "skipping" in m for m in caplog.messages)

        # Extraction draft was still filed
        assert len(result.extraction_drafts_created) == 1
        assert result.extraction_drafts_created[0]["title"] == "Extract X"

        # Alignment draft was skipped
        assert result.alignment_drafts_created == []

        db.reset_engine()

    def test_todo_draft_unknown_target_repo_id(self, tmp_path, monkeypatch, caplog):
        """A TODO draft targeting a non-existent (or missing) repo_id is
        skipped with a warning; other drafts are still filed."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")
        db.init_db(settings, board_id="repo-a")

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {},
        )

        agent_result = MetaAgentResult(
            updated_memory="ledger",
            extraction_drafts=[
                DraftProposal(
                    title="Extract X",
                    body="body",
                    target_repo_id=None,
                ),
            ],
            todo_drafts=[
                DraftProposal(
                    title="Bad todo",
                    body="bad body",
                    target_repo_id="no-such-repo",
                ),
                DraftProposal(
                    title="Missing target todo",
                    body="no target",
                    target_repo_id=None,
                ),
            ],
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.run_meta_agent",
            lambda **kw: agent_result,
        )

        reg = ReposRegistry(
            repos={
                "repo-a": _repo_cfg("repo-a", board_id="repo-a"),
            }
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.get_repos_config", lambda: reg)

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.persist_memory", lambda p, t: None
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.load_memory", lambda _p: "")
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "",
        )

        with caplog.at_level(logging.WARNING):
            result = run_meta_pass("test-session")

        # Warning logged about unknown repo and missing target
        assert any("no-such-repo" in m and "skipping" in m for m in caplog.messages)
        assert any(
            "no target_repo_id" in m and "skipping" in m for m in caplog.messages
        )

        # Extraction draft was still filed
        assert len(result.extraction_drafts_created) == 1
        assert result.extraction_drafts_created[0]["title"] == "Extract X"

        # Both TODO drafts were skipped
        assert result.todo_drafts_created == []

        db.reset_engine()

    def test_trace_review_target_repo_id_unset_graceful(
        self, tmp_path, monkeypatch, caplog
    ):
        """When trace_review_target_repo_id is empty, force_traces_to_mill
        is NOT called and the pass still completes (graceful degradation)."""
        settings = _make_settings(tmp_path, trace_review_target_repo_id="")
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {},
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.run_meta_agent",
            lambda **kw: MetaAgentResult(updated_memory="ok"),
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.persist_memory", lambda p, t: None
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.load_memory", lambda _p: "")
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "",
        )

        # force_traces_to_mill should NOT be called — prove by
        # replacing it with something that raises if called.
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.force_traces_to_mill",
            lambda _rc: (_ for _ in ()).throw(
                AssertionError("force_traces_to_mill was called!")
            ),
        )

        with caplog.at_level(logging.INFO):
            result = run_meta_pass("test-session")

        assert result.updated_memory == "ok"
        assert any(
            "trace_review_target_repo_id not configured" in m for m in caplog.messages
        )

        db.reset_engine()

    def test_trace_review_target_repo_id_set_uses_force_traces(
        self, tmp_path, monkeypatch
    ):
        """When trace_review_target_repo_id is set and the repo exists
        in repos config, force_traces_to_mill is called."""
        settings = _make_settings(tmp_path, trace_review_target_repo_id="mill-repo")
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {},
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.run_meta_agent",
            lambda **kw: MetaAgentResult(updated_memory="ok"),
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.persist_memory", lambda p, t: None
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.load_memory", lambda _p: "")
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "",
        )

        reg = ReposRegistry(
            repos={
                "mill-repo": _repo_cfg("mill-repo"),
            }
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.get_repos_config", lambda: reg)

        # Monkeypatch _ensure_tracing so the real context manager
        # doesn't try to set up real OTLP exporters.
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing._ensure_tracing",
            lambda repo_config=None: None,
        )

        ftc_called: list[RepoConfig] = []

        _real_ftc = __import__(
            "robotsix_mill.runtime.tracing", fromlist=["force_traces_to_mill"]
        ).force_traces_to_mill

        def _fake_ftc(rc):
            ftc_called.append(rc)
            # Return a real context manager that does nothing
            from contextlib import nullcontext

            return nullcontext()

        monkeypatch.setattr("robotsix_mill.meta.runner.force_traces_to_mill", _fake_ftc)

        result = run_meta_pass("test-session")

        assert result.updated_memory == "ok"
        assert len(ftc_called) == 1
        assert ftc_called[0].repo_id == "mill-repo"

        db.reset_engine()

    def test_create_raises_does_not_block_other_drafts(
        self, tmp_path, monkeypatch, caplog
    ):
        """When TicketService.create raises for one draft, the
        exception is logged and remaining drafts are still processed."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")
        db.init_db(settings, board_id="repo-b")

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {},
        )

        agent_result = MetaAgentResult(
            updated_memory="ledger",
            extraction_drafts=[
                DraftProposal(
                    title="Extract X",
                    body="body X",
                    target_repo_id=None,
                ),
                DraftProposal(
                    title="Extract Y",
                    body="body Y",
                    target_repo_id=None,
                ),
            ],
            alignment_drafts=[
                DraftProposal(
                    title="Align Z",
                    body="body Z",
                    target_repo_id="repo-b",
                ),
            ],
        )
        monkeypatch.setattr(
            "robotsix_mill.meta.runner.run_meta_agent",
            lambda **kw: agent_result,
        )

        reg = ReposRegistry(
            repos={
                "repo-b": _repo_cfg("repo-b", board_id="repo-b"),
            }
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.get_repos_config", lambda: reg)

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.persist_memory", lambda p, t: None
        )
        monkeypatch.setattr("robotsix_mill.meta.runner.load_memory", lambda _p: "")
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "",
        )

        # Make TicketService.create fail for the first extraction draft
        _real_create = TicketService.create
        call_count = [0]

        def _failing_create(
            self,
            title,
            description="",
            source=SourceKind.USER,
            origin_session=None,
            depends_on=None,
            kind=TicketKind.TASK,
            parent_id=None,
            board_id=None,
        ):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("DB exploded")
            return _real_create(
                self,
                title,
                description=description,
                source=source,
                origin_session=origin_session,
                depends_on=depends_on,
                kind=kind,
                parent_id=parent_id,
                board_id=board_id,
            )

        monkeypatch.setattr(
            "robotsix_mill.core.service.TicketService.create",
            _failing_create,
        )

        with caplog.at_level(logging.ERROR):
            result = run_meta_pass("test-session")

        # First draft failed
        assert any(
            "failed to create extraction draft" in m and "Extract X" in m
            for m in caplog.messages
        )

        # Second extraction draft succeeded
        assert len(result.extraction_drafts_created) == 1
        assert result.extraction_drafts_created[0]["title"] == "Extract Y"

        # Alignment draft also succeeded
        assert len(result.alignment_drafts_created) == 1
        assert result.alignment_drafts_created[0]["title"] == "Align Z"

        db.reset_engine()

    def test_memory_file_does_not_exist(self, tmp_path, monkeypatch):
        """When the memory file does not exist, load_memory returns ''
        and the agent still runs successfully."""
        settings = _make_settings(tmp_path)
        db.reset_engine()
        db.init_db(settings, board_id="meta")

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.Settings",
            lambda: settings,
        )

        monkeypatch.setattr(
            "robotsix_mill.meta.runner.clone_all_repos",
            lambda _s: {},
        )

        captured_memory: list[str] = []

        def _fake_agent(**kw):
            captured_memory.append(kw["memory"])
            return MetaAgentResult(updated_memory="first memory")

        monkeypatch.setattr("robotsix_mill.meta.runner.run_meta_agent", _fake_agent)

        persist_calls: list[tuple[Path, str]] = []

        def _fake_persist(path, text):
            persist_calls.append((path, text))

        monkeypatch.setattr("robotsix_mill.meta.runner.persist_memory", _fake_persist)
        monkeypatch.setattr(
            "robotsix_mill.meta.runner._gather_meta_proposals",
            lambda _s: "",
        )

        # Do NOT mock load_memory — use the real one.  The memory
        # file does not exist on disk, so load_memory returns "".

        result = run_meta_pass("test-session")

        assert captured_memory == [""]
        assert result.updated_memory == "first memory"
        assert len(persist_calls) == 1
        assert persist_calls[0][1] == "first memory"

        db.reset_engine()
