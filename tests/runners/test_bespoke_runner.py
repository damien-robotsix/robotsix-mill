"""Tests for the bespoke-agent pass runner.

Mirrors the audit-runner test pattern: monkeypatch the inner agent
seam so no LLM / network is involved, then assert that the runner
handles memory persistence, draft-ticket creation with the right
``source: bespoke:<name>`` label, and isolation between bespoke
agents on the same repo.
"""

from __future__ import annotations


from robotsix_mill.agents import bespoke as _bespoke
from robotsix_mill.agents.bespoke import BespokeResult
from robotsix_mill.agents.bespoke_loader import BespokeAgentDefinition
from robotsix_mill.runners.bespoke_runner import (
    BespokePassResult,
    _memory_file_for,
    run_bespoke_pass,
)
from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService


def _settings(tmp_path, **overrides):
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    _reset_secrets()
    return s


def _test_repo_config():
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _definition(**overrides) -> BespokeAgentDefinition:
    base = {
        "name": "mail-checker",
        "interval_seconds": 3600,
        "system_prompt": "You inspect the mail subsystem.",
    }
    base.update(overrides)
    return BespokeAgentDefinition.model_validate(base)


# ---------------------------------------------------------------------------
#  Memory-file resolution
# ---------------------------------------------------------------------------


class TestMemoryFile:
    def test_per_board_per_agent_path(self, tmp_path):
        """Each bespoke agent has its own memory ledger under the
        target repo's data subdir — isolated from mill core's
        per-agent ledgers AND from other repos' bespoke agents on
        the same name."""
        s = _settings(tmp_path)
        p = _memory_file_for(s, "robotsix-auto-mail", "mail-checker")
        assert p == (
            s.data_dir / "robotsix-auto-mail" / "bespoke_mail-checker_memory.md"
        )

    def test_no_board_falls_back_to_data_dir(self, tmp_path):
        """Single-repo / legacy callers without a board_id still get
        a writable ledger location — the bespoke runner must work
        in single-repo mode too."""
        s = _settings(tmp_path)
        p = _memory_file_for(s, "", "mail-checker")
        assert p == s.data_dir / "bespoke_mail-checker_memory.md"


# ---------------------------------------------------------------------------
#  Pass orchestration
# ---------------------------------------------------------------------------


class TestRunBespokePass:
    def test_pass_creates_draft_with_per_agent_source_label(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Drafts emitted by the bespoke pass carry ``source:
        bespoke:<name>``. The per-agent label is the dedup key — two
        bespoke agents on the same repo file independently and never
        cross-eat each other's prior proposals."""
        s = _settings(tmp_path)
        monkeypatch.setattr(
            "robotsix_mill.runners.bespoke_runner.Settings",
            lambda: s,
        )

        def fake_agent(**kwargs):
            return BespokeResult(
                updated_memory="seen-one",
                draft_titles=["IMAP TLS regression"],
                draft_bodies=["Body content"],
                gap_ids=["imap_tls"],
            )

        monkeypatch.setattr(
            _bespoke,
            "run_bespoke_agent",
            fake_agent,
        )

        result = run_bespoke_pass(
            session_id="sess-1",
            definition=_definition(name="mail-checker"),
            repo_config=_test_repo_config(),
            repo_dir=None,
        )

        assert isinstance(result, BespokePassResult)
        assert result.source_label == "bespoke:mail-checker"
        assert len(result.drafts_created) == 1

        # Ticket carries the per-agent source label, not a generic
        # "bespoke" lumping. Future dedup queries hit only this
        # agent's history.
        svc = TicketService(s, board_id="test-board")
        tickets = svc.list()
        assert len(tickets) == 1
        assert tickets[0].source == "bespoke:mail-checker"

    def test_two_bespoke_agents_keep_separate_memories(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Two bespoke agents on the same repo each have their own
        memory ledger and their own dedup history. A draft filed by
        agent A is not surfaced as a prior proposal to agent B."""
        s = _settings(tmp_path)
        monkeypatch.setattr(
            "robotsix_mill.runners.bespoke_runner.Settings",
            lambda: s,
        )

        captured_memory: dict[str, str] = {}

        def fake_agent(**kwargs):
            definition = kwargs.get("definition")
            captured_memory[definition.name] = kwargs.get("memory", "")
            return BespokeResult(
                updated_memory=f"after-{definition.name}",
                draft_titles=[],
                draft_bodies=[],
                gap_ids=[],
            )

        monkeypatch.setattr(
            _bespoke,
            "run_bespoke_agent",
            fake_agent,
        )

        run_bespoke_pass(
            session_id="s1",
            definition=_definition(name="agent-a"),
            repo_config=_test_repo_config(),
            repo_dir=None,
        )
        run_bespoke_pass(
            session_id="s2",
            definition=_definition(name="agent-b"),
            repo_config=_test_repo_config(),
            repo_dir=None,
        )
        # A second run of agent-a should see ITS own memory, not B's.
        run_bespoke_pass(
            session_id="s3",
            definition=_definition(name="agent-a"),
            repo_config=_test_repo_config(),
            repo_dir=None,
        )

        # The third call was agent-a; the memory it read must be the
        # one agent-a wrote on its first invocation, not anything
        # agent-b wrote in between.
        assert captured_memory["agent-a"] == "after-agent-a"

    def test_agent_invocation_receives_definition_and_repo_dir(
        self,
        tmp_path,
        monkeypatch,
    ):
        """The bespoke runner forwards the definition + clone path to
        the inner agent function via ``functools.partial``. Guards
        against a future change that silently drops one of them and
        leaves the bespoke agent running with no clone / no prompt."""
        s = _settings(tmp_path)
        monkeypatch.setattr(
            "robotsix_mill.runners.bespoke_runner.Settings",
            lambda: s,
        )
        clone = tmp_path / "fake-clone"
        clone.mkdir()
        captured: dict = {}

        def fake_agent(**kwargs):
            captured.update(kwargs)
            return BespokeResult()

        monkeypatch.setattr(
            _bespoke,
            "run_bespoke_agent",
            fake_agent,
        )

        run_bespoke_pass(
            session_id="s1",
            definition=_definition(name="mail-checker"),
            repo_config=_test_repo_config(),
            repo_dir=clone,
        )

        assert captured["definition"].name == "mail-checker"
        assert captured["repo_dir"] == clone
