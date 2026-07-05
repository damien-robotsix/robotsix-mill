"""Unit tests for ``_reconcile`` module functions.

Covers the artifact-writing helpers, short-circuit guards, and
side-effect applicators in ``src/robotsix_mill/stages/refine/_reconcile.py``.
All agent / external collaborators are mocked.
"""

from __future__ import annotations

import json

import pytest

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    FileMapEntry,
    RefineResult,
    ReviewerAgreementResult,
    TriageResult,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import _reconcile
from robotsix_mill.stages.refine import orchestration as orch_module
from robotsix_mill.vcs import git_ops


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path):
    from robotsix_mill.config import RepoConfig, Settings

    counter = [0]

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{counter[0]}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        counter[0] += 1
        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


def _ticket(ctx, title="Add feature", body=None, **kw):
    """Create a DRAFT ticket with a substantive body."""
    if body is None:
        body = "Add a feature. This is a substantive draft body."
    return ctx.service.create(title, body, **kw)


def _ws(ctx, ticket):
    """Return the workspace for *ticket*."""
    return ctx.service.workspace(ticket)


# ===========================================================================
# write_triage_complexity
# ===========================================================================


class TestWriteTriageComplexity:
    def test_basic_write(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        _reconcile.write_triage_complexity(ws, "simple")

        path = ws.artifacts_dir / "triage_complexity.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"complexity": "simple"}
        # findings file NOT written when findings is None/empty
        assert not (ws.artifacts_dir / "triage_findings.json").exists()

    def test_with_trivial_scope(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        _reconcile.write_triage_complexity(ws, "simple", trivial_scope=True)

        data = json.loads(
            (ws.artifacts_dir / "triage_complexity.json").read_text(encoding="utf-8")
        )
        assert data == {"complexity": "simple", "trivial_scope": True}

    def test_with_findings_writes_findings_file(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        _reconcile.write_triage_complexity(
            ws, "complex", findings="needs deep exploration of module X"
        )

        findings_path = ws.artifacts_dir / "triage_findings.json"
        assert findings_path.exists()
        findings_data = json.loads(findings_path.read_text(encoding="utf-8"))
        assert findings_data == {"findings": "needs deep exploration of module X"}

    def test_with_all_params(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        _reconcile.write_triage_complexity(
            ws, "complex", trivial_scope=False, findings="several submodules"
        )

        complexity_data = json.loads(
            (ws.artifacts_dir / "triage_complexity.json").read_text(encoding="utf-8")
        )
        assert complexity_data == {"complexity": "complex", "trivial_scope": False}
        findings_data = json.loads(
            (ws.artifacts_dir / "triage_findings.json").read_text(encoding="utf-8")
        )
        assert findings_data == {"findings": "several submodules"}

    def test_trivial_scope_none_omitted(self, ctx_factory):
        """When trivial_scope is None (default), it is NOT written to JSON."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        _reconcile.write_triage_complexity(ws, "simple", trivial_scope=None)

        data = json.loads(
            (ws.artifacts_dir / "triage_complexity.json").read_text(encoding="utf-8")
        )
        assert "trivial_scope" not in data

    def test_empty_findings_does_not_write_findings_file(self, ctx_factory):
        """Empty string findings is falsy — no triage_findings.json written."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        _reconcile.write_triage_complexity(ws, "simple", findings="")

        assert not (ws.artifacts_dir / "triage_findings.json").exists()


# ===========================================================================
# read_triage_complexity
# ===========================================================================


class TestReadTriageComplexity:
    def test_reads_complexity(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        _reconcile.write_triage_complexity(ws, "simple")

        result = _reconcile.read_triage_complexity(ws)

        assert result == "simple"

    def test_missing_file_returns_default(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        result = _reconcile.read_triage_complexity(ws)

        assert result == "needs-exploration"

    def test_corrupt_json_returns_default(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        (ws.artifacts_dir / "triage_complexity.json").write_text(
            "not valid json {{{", encoding="utf-8"
        )

        result = _reconcile.read_triage_complexity(ws)

        assert result == "needs-exploration"

    def test_missing_complexity_key_returns_default(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        (ws.artifacts_dir / "triage_complexity.json").write_text(
            json.dumps({"other": "value"}), encoding="utf-8"
        )

        result = _reconcile.read_triage_complexity(ws)

        assert result == "needs-exploration"


# ===========================================================================
# read_triage_findings
# ===========================================================================


class TestReadTriageFindings:
    def test_reads_findings(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        _reconcile.write_triage_complexity(
            ws, "complex", findings="explore the auth module"
        )

        result = _reconcile.read_triage_findings(ws)

        assert result == "explore the auth module"

    def test_missing_file_returns_none(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        result = _reconcile.read_triage_findings(ws)

        assert result is None

    def test_corrupt_json_returns_none(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        (ws.artifacts_dir / "triage_findings.json").write_text(
            "bad json", encoding="utf-8"
        )

        result = _reconcile.read_triage_findings(ws)

        assert result is None

    def test_missing_findings_key_returns_none(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        (ws.artifacts_dir / "triage_findings.json").write_text(
            json.dumps({"other": "data"}), encoding="utf-8"
        )

        result = _reconcile.read_triage_findings(ws)

        assert result is None

    def test_findings_is_empty_string_returns_none(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        (ws.artifacts_dir / "triage_findings.json").write_text(
            json.dumps({"findings": ""}), encoding="utf-8"
        )

        result = _reconcile.read_triage_findings(ws)

        assert result is None


# ===========================================================================
# read_triage_trivial
# ===========================================================================


class TestReadTriageTrivial:
    def test_reads_true(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        _reconcile.write_triage_complexity(ws, "simple", trivial_scope=True)

        result = _reconcile.read_triage_trivial(ws)

        assert result is True

    def test_reads_false(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        _reconcile.write_triage_complexity(ws, "complex", trivial_scope=False)

        result = _reconcile.read_triage_trivial(ws)

        assert result is False

    def test_missing_file_returns_false(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        result = _reconcile.read_triage_trivial(ws)

        assert result is False

    def test_corrupt_json_returns_false(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        (ws.artifacts_dir / "triage_complexity.json").write_text(
            "invalid", encoding="utf-8"
        )

        result = _reconcile.read_triage_trivial(ws)

        assert result is False

    def test_missing_trivial_scope_key_returns_false(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        (ws.artifacts_dir / "triage_complexity.json").write_text(
            json.dumps({"complexity": "simple"}), encoding="utf-8"
        )

        result = _reconcile.read_triage_trivial(ws)

        assert result is False


# ===========================================================================
# persist_triage_complexity
# ===========================================================================


class TestPersistTriageComplexity:
    def test_persists_from_triage_result(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        triage = TriageResult(
            decision="REFINE",
            reason="needs work",
            complexity="simple",
            trivial_scope=True,
            exploration_findings="check the auth module",
        )

        _reconcile.persist_triage_complexity(ws, triage)

        assert _reconcile.read_triage_complexity(ws) == "simple"
        assert _reconcile.read_triage_trivial(ws) is True
        assert _reconcile.read_triage_findings(ws) == "check the auth module"

    def test_complexity_none_defaults_to_needs_exploration(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        triage = TriageResult(
            decision="REFINE",
            reason="needs work",
            complexity=None,
        )

        _reconcile.persist_triage_complexity(ws, triage)

        assert _reconcile.read_triage_complexity(ws) == "needs-exploration"

    def test_minimal_triage_result(self, ctx_factory):
        """TriageResult with only decision and reason — no extra fields."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        triage = TriageResult(decision="SKIP", reason="precise draft")

        _reconcile.persist_triage_complexity(ws, triage)

        assert _reconcile.read_triage_complexity(ws) == "needs-exploration"
        assert _reconcile.read_triage_trivial(ws) is False
        assert _reconcile.read_triage_findings(ws) is None


# ===========================================================================
# write_file_map
# ===========================================================================


class TestWriteFileMap:
    def test_writes_entries(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        entries = [{"file": "src/a.py", "note": "main logic"}]

        _reconcile.write_file_map(ws, entries)

        path = ws.artifacts_dir / "file_map.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == entries

    def test_writes_empty_entries(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)

        _reconcile.write_file_map(ws, [])

        path = ws.artifacts_dir / "file_map.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == []

    def test_only_if_absent_skips_when_file_exists(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        # Pre-write a file.
        original = [{"file": "existing.py", "note": "already there"}]
        _reconcile.write_file_map(ws, original)

        # Now try with only_if_absent=True — should be a no-op.
        _reconcile.write_file_map(
            ws, [{"file": "new.py", "note": "should not appear"}], only_if_absent=True
        )

        data = json.loads(
            (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
        )
        assert data == original

    def test_only_if_absent_writes_when_file_absent(self, ctx_factory):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        entries = [{"file": "src/b.py", "note": "new entry"}]

        _reconcile.write_file_map(ws, entries, only_if_absent=True)

        data = json.loads(
            (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
        )
        assert data == entries


# ===========================================================================
# short_circuit_for_internal_failure
# ===========================================================================


class TestShortCircuitForInternalFailure:
    def test_happy_path_returns_outcome(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        draft = "Traceback (most recent call last):\n  ...\nTypeError: ..."

        monkeypatch.setattr(refining, "is_internal_toolchain_failure", lambda d: True)

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx, t, draft, ws, s, reviewer_comments=None
        )

        assert outcome is not None
        assert outcome.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
        assert "short-circuited" in outcome.note
        # Spec was written to workspace.
        assert ws.description_path.exists()
        # draft-original.md was written.
        assert (ws.artifacts_dir / "draft-original.md").exists()
        # file_map.json was written (empty, only_if_absent).
        assert (ws.artifacts_dir / "file_map.json").exists()
        # triage_complexity.json was written.
        assert (ws.artifacts_dir / "triage_complexity.json").exists()

    def test_reviewer_comments_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx,
            t,
            "Traceback...",
            ws,
            s,
            reviewer_comments="Please fix the scope too.",
        )

        assert outcome is None

    def test_empty_draft_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx, t, "", ws, s, reviewer_comments=None
        )

        assert outcome is None

    def test_whitespace_only_draft_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx, t, "   \n  ", ws, s, reviewer_comments=None
        )

        assert outcome is None

    def test_not_internal_failure_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(refining, "is_internal_toolchain_failure", lambda d: False)

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx,
            t,
            "A normal feature request draft.",
            ws,
            s,
            reviewer_comments=None,
        )

        assert outcome is None

    def test_with_evidence_txt_includes_evidence(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        # Write evidence.txt BEFORE calling.
        evidence_text = "CI log line 1\nCI log line 2\n" * 10
        (ws.artifacts_dir / "evidence.txt").write_text(evidence_text, encoding="utf-8")

        monkeypatch.setattr(refining, "is_internal_toolchain_failure", lambda d: True)

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx, t, "pytest FAILED test_foo", ws, s, reviewer_comments=None
        )

        assert outcome is not None
        spec = ws.description_path.read_text(encoding="utf-8")
        assert "evidence.txt" in spec
        # Evidence content appears (truncated to 4000 chars).
        assert "CI log line 1" in spec

    def test_evidence_txt_read_error_continues(self, ctx_factory, monkeypatch):
        """If evidence.txt can't be read, the function still produces an Outcome."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        # Create evidence.txt but we'll mock path.read_text to fail...
        # Actually, the function uses evidence_path.read_text() which we can't
        # easily mock per-instance. Instead, make evidence.txt a directory
        # so reading it raises an error.
        evidence_path = ws.artifacts_dir / "evidence.txt"
        evidence_path.mkdir()  # directory, not file — read_text() will fail

        monkeypatch.setattr(refining, "is_internal_toolchain_failure", lambda d: True)

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx, t, "pytest FAILED", ws, s, reviewer_comments=None
        )

        assert outcome is not None  # still produced
        spec = ws.description_path.read_text(encoding="utf-8")
        assert "evidence.txt" not in spec  # evidence note omitted

    def test_draft_truncation_applied(self, ctx_factory, monkeypatch):
        """Long drafts (>3000 chars) are truncated in the generated spec."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        long_draft = "x" * 5000

        monkeypatch.setattr(refining, "is_internal_toolchain_failure", lambda d: True)

        outcome = _reconcile.short_circuit_for_internal_failure(
            ctx, t, long_draft, ws, s, reviewer_comments=None
        )

        assert outcome is not None
        spec = ws.description_path.read_text(encoding="utf-8")
        assert "… [truncated]" in spec


# ===========================================================================
# gitignored_guard
# ===========================================================================


class TestGitignoredGuard:
    def test_blocked_paths_returns_outcome(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        result = RefineResult(
            spec_markdown="## Problem\n\nFix it.",
            file_map=[FileMapEntry(file="src/vendored/x.py", note="main")],
        )
        monkeypatch.setattr(
            git_ops, "ignored_paths", lambda repo, paths: ["src/vendored/x.py"]
        )

        outcome = _reconcile.gitignored_guard(t, result, repo_dir=ctx.settings.data_dir)

        assert outcome is not None
        assert outcome.next_state == State.BLOCKED
        assert "src/vendored/x.py" in outcome.note

    def test_no_blocked_paths_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        result = RefineResult(
            spec_markdown="## Problem\n\nFix it.",
            file_map=[FileMapEntry(file="src/normal.py", note="main")],
        )
        monkeypatch.setattr(git_ops, "ignored_paths", lambda repo, paths: [])

        outcome = _reconcile.gitignored_guard(t, result, repo_dir=ctx.settings.data_dir)

        assert outcome is None

    def test_meta_board_skipped(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx, board_id="meta")
        result = RefineResult(
            spec_markdown="## Problem\n\nFix it.",
            file_map=[FileMapEntry(file="src/x.py", note="main")],
        )

        outcome = _reconcile.gitignored_guard(t, result, repo_dir=ctx.settings.data_dir)

        assert outcome is None

    def test_empty_file_map_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        result = RefineResult(
            spec_markdown="## Problem\n\nFix it.",
            file_map=[],
        )

        outcome = _reconcile.gitignored_guard(t, result, repo_dir=ctx.settings.data_dir)

        assert outcome is None

    def test_none_file_map_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        result = RefineResult(
            spec_markdown="## Problem\n\nFix it.",
            file_map=None,
        )

        outcome = _reconcile.gitignored_guard(t, result, repo_dir=ctx.settings.data_dir)

        assert outcome is None

    def test_repo_dir_none_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        result = RefineResult(
            spec_markdown="## Problem\n\nFix it.",
            file_map=[FileMapEntry(file="src/x.py", note="main")],
        )

        outcome = _reconcile.gitignored_guard(t, result, repo_dir=None)

        assert outcome is None

    def test_multiple_blocked_paths_in_note(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        result = RefineResult(
            spec_markdown="## Problem\n\nFix it.",
            file_map=[
                FileMapEntry(file="src/vendored/a.py", note="a"),
                FileMapEntry(file="src/vendored/b.py", note="b"),
            ],
        )
        monkeypatch.setattr(
            git_ops,
            "ignored_paths",
            lambda repo, paths: ["src/vendored/a.py", "src/vendored/b.py"],
        )

        outcome = _reconcile.gitignored_guard(t, result, repo_dir=ctx.settings.data_dir)

        assert outcome is not None
        assert outcome.next_state == State.BLOCKED
        assert "src/vendored/a.py" in outcome.note
        assert "src/vendored/b.py" in outcome.note


# ===========================================================================
# apply_agent_side_effects
# ===========================================================================


class TestApplyAgentSideEffects:
    def test_full_result_applies_all_side_effects(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        draft = "Original draft body here."

        # Mock the orchestration memory persistence.
        memory_calls: list[dict] = []

        def _fake_persist_memory(settings, memory_board_id, text):
            memory_calls.append(
                {"settings": settings, "board_id": memory_board_id, "text": text}
            )

        monkeypatch.setattr(orch_module, "_persist_refine_memory", _fake_persist_memory)

        result = RefineResult(
            spec_markdown="## Problem\n\nFixed.",
            updated_memory="New memory content",
            title="Better Title",
            epic_body=None,
            file_map=[
                FileMapEntry(file="src/a.py", note="core"),
                FileMapEntry(file="tests/test_a.py", note="tests"),
            ],
            reference_files=["docs/guide.md", "README.md"],
        )

        _reconcile.apply_agent_side_effects(
            ctx, t, draft, ws, s, epic_ctx="", result=result
        )

        # Memory was persisted.
        assert len(memory_calls) == 1
        assert memory_calls[0]["text"] == "New memory content"

        # Title was set.
        updated_ticket = ctx.service.get(t.id)
        assert updated_ticket.title == "Better Title"

        # Draft was preserved.
        draft_original = ws.artifacts_dir / "draft-original.md"
        assert draft_original.exists()
        assert draft_original.read_text(encoding="utf-8") == draft

        # File map was written.
        file_map_path = ws.artifacts_dir / "file_map.json"
        assert file_map_path.exists()
        file_map_data = json.loads(file_map_path.read_text(encoding="utf-8"))
        assert {"file": "src/a.py", "note": "core"} in file_map_data
        assert {"file": "tests/test_a.py", "note": "tests"} in file_map_data

        # Reference files were written.
        ref_path = ws.artifacts_dir / "reference_files.json"
        assert ref_path.exists()
        ref_data = json.loads(ref_path.read_text(encoding="utf-8"))
        assert {"path": "docs/guide.md"} in ref_data
        assert {"path": "README.md"} in ref_data

    def test_no_updated_memory_skips_memory_persist(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        memory_calls: list[dict] = []

        def _fake_persist_memory(settings, memory_board_id, text):
            memory_calls.append({})

        monkeypatch.setattr(orch_module, "_persist_refine_memory", _fake_persist_memory)

        result = RefineResult(spec_markdown="## X", updated_memory="")

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="", result=result
        )

        assert len(memory_calls) == 0

    def test_no_title_skips_title_update(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        original_title = t.title

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(spec_markdown="## X", title="")

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="", result=result
        )

        updated_ticket = ctx.service.get(t.id)
        assert updated_ticket.title == original_title

    def test_whitespace_title_skipped(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        original_title = t.title

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(spec_markdown="## X", title="   ")

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="", result=result
        )

        updated_ticket = ctx.service.get(t.id)
        assert updated_ticket.title == original_title

    def test_epic_body_with_non_epic_parent_skips(self, ctx_factory, monkeypatch):
        """parent_id points to a non-EPIC ticket — epic_body is skipped."""
        ctx = ctx_factory()
        parent = _ticket(ctx, title="Parent task")
        t = _ticket(ctx, title="Child", parent_id=parent.id)  # parent is TASK, not EPIC
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(
            spec_markdown="## X",
            epic_body="Should not be applied",
        )

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="some-epic-ctx", result=result
        )

        # Parent's description should NOT have been updated.
        parent_ws = _ws(ctx, parent)
        assert "Should not be applied" not in parent_ws.read_description()

    def test_epic_body_with_epic_parent_no_approval_writes_directly(
        self, ctx_factory, monkeypatch
    ):
        """When require_approval=False, epic_body is written to parent directly."""
        ctx = ctx_factory(require_approval=False)
        parent = _ticket(ctx, title="Epic Parent", kind=TicketKind.EPIC)
        t = _ticket(ctx, title="Child", parent_id=parent.id)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(
            spec_markdown="## X",
            epic_body="Updated epic strategy.",
        )

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="present", result=result
        )

        parent_ws = _ws(ctx, parent)
        assert "Updated epic strategy." in parent_ws.read_description()

    def test_epic_body_with_approval_writes_artifact(self, ctx_factory, monkeypatch):
        """When require_approval=True, epic_body is stored as an artifact."""
        ctx = ctx_factory(require_approval=True)
        parent = _ticket(ctx, title="Epic Parent", kind=TicketKind.EPIC)
        t = _ticket(ctx, title="Child", parent_id=parent.id)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(
            spec_markdown="## X",
            epic_body="Proposed epic update.",
        )

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="present", result=result
        )

        artifact = ws.artifacts_dir / "epic-body-proposed.md"
        assert artifact.exists()
        assert artifact.read_text(encoding="utf-8") == "Proposed epic update."

    def test_epic_body_no_epic_ctx_skips(self, ctx_factory, monkeypatch):
        """When epic_ctx is falsy, epic_body logic is skipped entirely."""
        ctx = ctx_factory(require_approval=False)
        parent = _ticket(ctx, title="Epic Parent", kind=TicketKind.EPIC)
        t = _ticket(ctx, title="Child", parent_id=parent.id)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(
            spec_markdown="## X",
            epic_body="Should be skipped.",
        )

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="", result=result
        )

        parent_ws = _ws(ctx, parent)
        assert "Should be skipped" not in parent_ws.read_description()
        assert not (ws.artifacts_dir / "epic-body-proposed.md").exists()

    def test_no_file_map_skips_file_map_write(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(spec_markdown="## X", file_map=None)

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="", result=result
        )

        assert not (ws.artifacts_dir / "file_map.json").exists()

    def test_no_reference_files_skips_reference_files_write(
        self, ctx_factory, monkeypatch
    ):
        """Empty reference_files list is falsy — no file written."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(spec_markdown="## X", reference_files=[])

        _reconcile.apply_agent_side_effects(
            ctx, t, "draft", ws, s, epic_ctx="", result=result
        )

        assert not (ws.artifacts_dir / "reference_files.json").exists()

    def test_none_draft_preserved_with_placeholder(self, ctx_factory, monkeypatch):
        """When draft is None/empty, a placeholder is written."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(orch_module, "_persist_refine_memory", lambda s, b, t: None)

        result = RefineResult(spec_markdown="## X")

        _reconcile.apply_agent_side_effects(
            ctx, t, "", ws, s, epic_ctx="", result=result
        )

        draft_original = ws.artifacts_dir / "draft-original.md"
        assert draft_original.exists()
        assert "title-only" in draft_original.read_text(encoding="utf-8")


# ===========================================================================
# reviewer_agreement_guard
# ===========================================================================


class TestReviewerAgreementGuard:
    def test_agree_task_no_branch_routes_to_implement(self, ctx_factory, monkeypatch):
        """AGREE for a TASK without a branch → routes to implement (not DONE)."""
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        reviewer_comments = "Confirmed — the draft is correct, no change needed."

        monkeypatch.setattr(
            refining,
            "triage_reviewer_agreement",
            lambda **kw: ReviewerAgreementResult(
                decision="AGREE", reason="Reviewer confirms no change needed."
            ),
        )

        outcome = _reconcile.reviewer_agreement_guard(
            ctx, t, "some draft", ws, s, reviewer_comments
        )

        assert outcome is not None
        assert outcome.next_state is not State.DONE
        assert "reviewer agreement" in outcome.note.lower()
        # Artifacts written.
        assert (ws.artifacts_dir / "draft-original.md").exists()
        assert (ws.artifacts_dir / "file_map.json").exists()

    def test_agree_task_with_branch_routes_to_done(self, ctx_factory, monkeypatch):
        """AGREE for a TASK with a branch → DONE."""
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ctx.service.set_branch(t.id, "feat/some-branch")
        t = ctx.service.get(t.id)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(
            refining,
            "triage_reviewer_agreement",
            lambda **kw: ReviewerAgreementResult(
                decision="AGREE", reason="Already implemented."
            ),
        )

        outcome = _reconcile.reviewer_agreement_guard(
            ctx, t, "some draft", ws, s, "LGTM"
        )

        assert outcome is not None
        assert outcome.next_state == State.DONE
        assert "no change needed" in outcome.note

    def test_disagree_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        monkeypatch.setattr(
            refining,
            "triage_reviewer_agreement",
            lambda **kw: ReviewerAgreementResult(
                decision="DISAGREE", reason="Reviewer wants changes."
            ),
        )

        outcome = _reconcile.reviewer_agreement_guard(
            ctx, t, "some draft", ws, s, "Please update the README."
        )

        assert outcome is None

    def test_gate_disabled_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=False, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        triage_called: list[dict] = []

        def _record(**kw):
            triage_called.append(kw)
            return ReviewerAgreementResult(decision="AGREE", reason="ok")

        monkeypatch.setattr(refining, "triage_reviewer_agreement", _record)

        outcome = _reconcile.reviewer_agreement_guard(ctx, t, "draft", ws, s, "LGTM")

        assert outcome is None
        assert triage_called == []  # never called

    def test_no_reviewer_comments_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        outcome = _reconcile.reviewer_agreement_guard(
            ctx, t, "draft", ws, s, reviewer_comments=None
        )

        assert outcome is None

    def test_empty_reviewer_comments_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        outcome = _reconcile.reviewer_agreement_guard(
            ctx, t, "draft", ws, s, reviewer_comments=""
        )

        assert outcome is None

    def test_refine_triage_disabled_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=False
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        outcome = _reconcile.reviewer_agreement_guard(ctx, t, "draft", ws, s, "LGTM")

        assert outcome is None

    def test_exception_falls_through_returns_none(self, ctx_factory, monkeypatch):
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings

        def _boom(**kw):
            raise RuntimeError("backend down")

        monkeypatch.setattr(refining, "triage_reviewer_agreement", _boom)

        outcome = _reconcile.reviewer_agreement_guard(ctx, t, "draft", ws, s, "LGTM")

        assert outcome is None

    def test_reason_truncation_long_reason(self, ctx_factory, monkeypatch):
        """Reason > 400 chars is truncated with ellipsis."""
        ctx = ctx_factory(
            reviewer_agreement_gate_enabled=True, refine_triage_enabled=True
        )
        t = _ticket(ctx)
        ws = _ws(ctx, t)
        s = ctx.settings
        long_reason = "x" * 500

        monkeypatch.setattr(
            refining,
            "triage_reviewer_agreement",
            lambda **kw: ReviewerAgreementResult(decision="AGREE", reason=long_reason),
        )

        outcome = _reconcile.reviewer_agreement_guard(ctx, t, "draft", ws, s, "LGTM")

        assert outcome is not None
        assert "…" in outcome.note
        # The truncated reason should be ≤ 401 chars of actual reason text.
        # The note contains the reason prefixed by other text, so just
        # check the ellipsis is present.
