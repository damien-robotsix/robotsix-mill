"""Tests for the agent-pass runner's filing guards, dedup and classification."""

from robotsix_mill.agents.runners.pass_runner import (
    _GAP_ID_RE,
    _POLICY_ADDITION_LABEL,
    _create_one_draft,
    _module_curator_premise_check,
    _render_verified_summary,
    _source_module_exists_for_gap,
    _test_file_exists_for_gap,
    canonical_gap_id,
    is_policy_addition,
    run_agent_pass,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import (
    SourceKind,
    TicketKind,
)
from robotsix_mill.core.service import TicketService, _parse_labels
from robotsix_mill.core.workspace import Workspace
from tests.agents.runners._pass_runner_fixtures import (
    _FakeAgentResult,
    _make_agent,
    _make_settings,
)

# --- gap-id regex tests for previously-unmatched labels ---


def test_gap_id_re_matches_bespoke_marker():
    """The _GAP_ID_RE must match bespoke:<name> markers so de-duplication works."""
    marker = "<!-- bespoke:my_agent-gap-id: abc123 -->"
    matches = _GAP_ID_RE.findall(marker)
    assert len(matches) == 1
    label, gap_id = matches[0]
    assert label == "bespoke:my_agent"
    assert gap_id == "abc123"


# --- test-gap live-filesystem guard tests ---


def test_test_file_exists_skips_ticket(tmp_path, monkeypatch):
    """When the expected test file already exists on disk, the
    test-gap draft is skipped with a warning."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create a fake test file on disk.
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing tests\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/coding.py"],
        draft_bodies=["Add tests for coding.py"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # No tickets created — the draft was skipped.
    assert result.drafts_created == []
    tickets = service.list()
    assert len(tickets) == 0

    db.reset_engine()


def test_test_file_absent_creates_ticket(tmp_path, monkeypatch):
    """When the expected test file does NOT exist, the test-gap draft
    is created normally."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # The source module must exist on disk so the source-module guard does
    # not suppress the draft (we are testing the test-file-absent path).
    src_module = tmp_path / "src" / "robotsix_mill" / "agents" / "nonexistent.py"
    src_module.parent.mkdir(parents=True, exist_ok=True)
    src_module.write_text("# source module\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/nonexistent.py"],
        draft_bodies=["Add tests for nonexistent.py"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket IS created.
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


def test_parse_failure_falls_through(tmp_path, monkeypatch):
    """Title that doesn't end in .py conservatively passes through."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for something weird"],
        draft_bodies=["Some body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket IS created (conservative pass-through).
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


def test_repo_dir_none_backward_compat(tmp_path, monkeypatch):
    """With repo_dir=None, the guard is never triggered — all drafts
    are created, matching pre-guard behavior."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create a test file on disk — it should NOT prevent ticket creation
    # since repo_dir is None (guard short-circuits).
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/coding.py"],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        # repo_dir NOT passed (defaults to None)
    )

    # Ticket created despite existing test file because repo_dir is None.
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


def test_non_test_gap_source_never_gated(tmp_path, monkeypatch):
    """SourceKind other than TEST_GAP never triggers the guard,
    regardless of repo_dir."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Existing test file — but source is AUDIT, not TEST_GAP.
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for agents/coding.py"],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket IS created — guard only fires for TEST_GAP.
    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


def test_bare_filename_module_path(tmp_path, monkeypatch):
    """Title with a bare filename (no directory) checks the correct path."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create the test file at tests/test_jscpd_tool.py
    test_file = tmp_path / "tests" / "test_jscpd_tool.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for jscpd_tool.py"],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket skipped — test file exists.
    assert result.drafts_created == []
    tickets = service.list()
    assert len(tickets) == 0

    db.reset_engine()


def test_full_path_module_stripping(tmp_path, monkeypatch):
    """Title with src/robotsix_mill/ prefix strips it before checking."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# existing\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=[
            "test gap: add unit tests for src/robotsix_mill/agents/coding.py"
        ],
        draft_bodies=["Body"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Ticket skipped — test file exists after prefix stripping.
    assert result.drafts_created == []
    tickets = service.list()
    assert len(tickets) == 0

    db.reset_engine()


# ------------------------------------------------------------------ _render_verified_summary direct tests


def test_render_verified_summary_empty():
    """Empty dict returns empty string."""
    result = _render_verified_summary({})
    assert result == ""


def test_render_verified_summary_counts():
    """Summary counts total and open proposals correctly."""
    verified = {
        "gap_1": {
            "ticket_id": "T-123",
            "state": "closed",
            "resolution": "merged",
            "branch": None,
        },
        "gap_2": {
            "ticket_id": "T-456",
            "state": "human_mr_approval",
            "resolution": "in-flight",
            "branch": None,
        },
    }
    result = _render_verified_summary(verified)
    assert "2 prior proposal(s) on file" in result
    assert "1 still open" in result
    assert "read_ticket" in result


def test_render_verified_summary_declined_not_open():
    """Declined proposals are not counted as open."""
    verified = {
        "gap_1": {
            "ticket_id": "T-789",
            "state": "closed",
            "resolution": "declined",
            "branch": None,
        },
    }
    result = _render_verified_summary(verified)
    assert "1 prior proposal(s) on file" in result
    assert "0 still open" in result


def test_render_verified_summary_branch_ignored():
    """Branch info is not in the summary (trimmed for context)."""
    verified = {
        "gap_1": {
            "ticket_id": "T-999",
            "state": "draft",
            "resolution": "in-flight",
            "branch": "feature/xyz",
        },
    }
    result = _render_verified_summary(verified)
    assert "feature/xyz" not in result
    assert "1 prior proposal(s) on file" in result
    assert "1 still open" in result


# ------------------------------------------------------------------ _test_file_exists_for_gap direct tests


def test_test_file_exists_for_gap_non_matching_title(tmp_path):
    """Title that doesn't match the pattern returns False."""
    assert _test_file_exists_for_gap(tmp_path, "some other title") is False


def test_test_file_exists_for_gap_no_py_extension(tmp_path):
    """Title without .py at the end returns False."""
    assert (
        _test_file_exists_for_gap(tmp_path, "test gap: add unit tests for foo/bar")
        is False
    )


def test_test_file_exists_for_gap_file_exists_directory_prefix(tmp_path):
    """Title with directory prefix - file exists → True."""
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# tests", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(
            tmp_path, "test gap: add unit tests for agents/coding.py"
        )
        is True
    )


def test_test_file_exists_for_gap_file_absent(tmp_path):
    """Title points to module with no test file → False."""
    # Ensure the directory exists but NOT the test file
    (tmp_path / "tests" / "agents").mkdir(parents=True, exist_ok=True)
    assert (
        _test_file_exists_for_gap(
            tmp_path, "test gap: add unit tests for agents/missing.py"
        )
        is False
    )


def test_test_file_exists_for_gap_prefix_stripping(tmp_path):
    """Title with src/robotsix_mill/ prefix — prefix stripped before check."""
    test_file = tmp_path / "tests" / "agents" / "test_coding.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# tests", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for src/robotsix_mill/agents/coding.py",
        )
        is True
    )


def test_test_file_exists_for_gap_bare_filename(tmp_path):
    """Bare filename (no directory) checks tests/test_<name>.py."""
    test_file = tmp_path / "tests" / "test_foo.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# tests", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(tmp_path, "test gap: add unit tests for foo.py")
        is True
    )


def test_test_file_exists_for_gap_bare_filename_not_in_subdir(tmp_path):
    """Documenting current limitation: bare filename only checks
    tests/test_<name>.py, NOT tests/subdir/test_<name>.py."""
    # Create file in subdirectory — should NOT be found by current impl
    subdir_file = tmp_path / "tests" / "subdir" / "test_foo.py"
    subdir_file.parent.mkdir(parents=True, exist_ok=True)
    subdir_file.write_text("# tests", encoding="utf-8")
    # The root tests/test_foo.py does NOT exist
    assert (
        _test_file_exists_for_gap(tmp_path, "test gap: add unit tests for foo.py")
        is False
    )


# --- runtime/routes/ fallback (HTTP-endpoint tested route modules) ---


def test_test_file_exists_for_gap_route_module_naming_convention(tmp_path):
    """A route module tested via the standard `test_<module>_routes.py`
    naming (not a 1:1 mirror) is detected by the fallback."""
    test_file = tmp_path / "tests" / "runtime" / "test_candidates_routes.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# candidates route tests\n", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for runtime/routes/_candidates.py",
        )
        is True
    )


def test_test_file_exists_for_gap_route_module_shared_file(tmp_path):
    """A route module tested in a shared file (referencing the token as a
    route path / test-function name) is detected by the fallback."""
    test_file = tmp_path / "tests" / "runtime" / "test_api.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "def test_health(client):\n    client.get('/health')\n",
        encoding="utf-8",
    )
    assert (
        _test_file_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for runtime/routes/_health.py",
        )
        is True
    )


def test_test_file_exists_for_gap_route_module_src_prefixed(tmp_path):
    """The fallback also fires for the full src/robotsix_mill/ prefixed form."""
    test_file = tmp_path / "tests" / "runtime" / "test_candidates_routes.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("# candidates route tests\n", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for "
            "src/robotsix_mill/runtime/routes/_candidates.py",
        )
        is True
    )


def test_test_file_exists_for_gap_route_module_genuinely_untested(tmp_path):
    """A route module with no matching test anywhere under tests/runtime/
    is NOT suppressed (conservative False)."""
    (tmp_path / "tests" / "runtime").mkdir(parents=True, exist_ok=True)
    assert (
        _test_file_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for runtime/routes/_nonexistent.py",
        )
        is False
    )


def test_test_file_exists_for_gap_non_routes_runtime_module_no_fallback(tmp_path):
    """A runtime/ module that is NOT under runtime/routes/ does not trigger
    the fallback — strict-mirror semantics are preserved."""
    # A test file under tests/runtime/ references the token, but the module
    # is runtime/server.py (not under routes/), so the fallback must not fire.
    test_file = tmp_path / "tests" / "runtime" / "test_server_routes.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_server():\n    pass\n", encoding="utf-8")
    assert (
        _test_file_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for runtime/server.py",
        )
        is False
    )


# ------------------------------------------------------------------ _source_module_exists_for_gap direct tests


def test_source_module_exists_for_gap_present_at_root(tmp_path):
    """Module present at the repo root → True."""
    module = tmp_path / "agents" / "coding.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# source\n", encoding="utf-8")
    assert (
        _source_module_exists_for_gap(
            tmp_path, "test gap: add unit tests for agents/coding.py"
        )
        is True
    )


def test_source_module_exists_for_gap_present_under_src(tmp_path):
    """Module present under src/<pkg>/ resolves via the package-root form."""
    module = (
        tmp_path / "src" / "robotsix_mill" / "stages" / "refine" / "orchestration.py"
    )
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# source\n", encoding="utf-8")
    assert (
        _source_module_exists_for_gap(
            tmp_path, "test gap: add unit tests for stages/refine/orchestration.py"
        )
        is True
    )


def test_source_module_exists_for_gap_full_src_prefixed_path(tmp_path):
    """The full src/robotsix_mill/-prefixed form resolves at the repo root."""
    module = tmp_path / "src" / "robotsix_mill" / "agents" / "coding.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# source\n", encoding="utf-8")
    assert (
        _source_module_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for src/robotsix_mill/agents/coding.py",
        )
        is True
    )


def test_source_module_exists_for_gap_absent(tmp_path):
    """Module absent from every candidate location → False."""
    (tmp_path / "src" / "robotsix_mill").mkdir(parents=True, exist_ok=True)
    assert (
        _source_module_exists_for_gap(
            tmp_path,
            "test gap: add unit tests for stages/refine/orchestration.py",
        )
        is False
    )


def test_source_module_exists_for_gap_non_matching_title(tmp_path):
    """A title that does not match the test-gap pattern passes through (True)."""
    assert _source_module_exists_for_gap(tmp_path, "some other title") is True


def test_source_module_exists_for_gap_non_py(tmp_path):
    """A module path that does not end in .py passes through (True)."""
    assert (
        _source_module_exists_for_gap(
            tmp_path, "test gap: add unit tests for something weird"
        )
        is True
    )


def test_source_module_exists_for_gap_strips_line_range(tmp_path):
    """A trailing :NN-NN line-range suffix is stripped before resolution."""
    module = tmp_path / "src" / "robotsix_mill" / "agents" / "coding.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# source\n", encoding="utf-8")
    assert (
        _source_module_exists_for_gap(
            tmp_path, "test gap: add unit tests for agents/coding.py:10-42"
        )
        is True
    )


# ------------------------------------------------------------------ source-module guard end-to-end


def test_absent_source_module_suppresses_test_gap_draft(tmp_path):
    """A test-gap draft whose source module is absent from the cloned tree
    (cross-repo misrouting) is suppressed — no ticket filed."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for stages/refine/orchestration.py"],
        draft_bodies=["Add tests for orchestration.py"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert result.drafts_created == []
    assert service.list() == []

    db.reset_engine()


def test_present_source_module_files_test_gap_draft(tmp_path):
    """A test-gap draft whose source module DOES exist under src/<pkg>/ is
    filed normally — legitimate gaps still create tickets."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    module = (
        tmp_path / "src" / "robotsix_mill" / "stages" / "refine" / "orchestration.py"
    )
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# source\n", encoding="utf-8")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["test gap: add unit tests for stages/refine/orchestration.py"],
        draft_bodies=["Add tests for orchestration.py"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    assert len(service.list()) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


# ==================================================================
# module_curator pre-filing premise check
# ==================================================================


def _write_modules_yaml(repo_dir, modules):
    """Write a minimal docs/modules.yaml under *repo_dir*."""
    import yaml as _yaml

    docs = repo_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "modules.yaml").write_text(
        _yaml.safe_dump({"modules": modules}), encoding="utf-8"
    )


# --- suppression: false file-missing premise (the 9c29 case) ---


def test_module_curator_suppresses_false_missing_premise(tmp_path, caplog):
    """A MODULE_CURATOR draft asserting docs/modules.yaml is missing is
    suppressed when the file actually exists under repo_dir."""
    import logging

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # The file the draft claims is missing actually exists.
    _write_modules_yaml(tmp_path, [{"id": "config", "paths": ["src/config.py"]}])

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=[
            "Create docs/modules.yaml: canonical module taxonomy is missing (CRITICAL)"
        ],
        draft_bodies=["docs/modules.yaml does not exist; create it."],
    )

    caplog.set_level(logging.WARNING)
    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert result.drafts_created == []
    assert service.list() == []
    assert "docs/modules.yaml" in caplog.text

    db.reset_engine()


def test_module_curator_files_when_missing_premise_is_true(tmp_path):
    """Counter-example: when the file really is absent, the create draft
    is filed normally (no suppression)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # No docs/modules.yaml on disk.
    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=[
            "Create docs/modules.yaml: canonical module taxonomy is missing (CRITICAL)"
        ],
        draft_bodies=["docs/modules.yaml does not exist; create it."],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    assert len(service.list()) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


# --- advisory: stale classify premise (the fdd7 case) ---


def test_module_curator_annotates_stale_classify(tmp_path):
    """A `Classify <file>:` draft whose file does not exist on HEAD is
    filed, but its body carries a [!warning] advisory block."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Classify src/robotsix_mill/gone.py: assign to a module"],
        draft_bodies=["The file src/robotsix_mill/gone.py is unclassified."],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC
    draft_tickets = [t for t in tickets if t.kind == TicketKind.TASK]
    assert len(draft_tickets) == 1
    desc = Workspace(
        settings.workspaces_dir_for("test-board"), draft_tickets[0].id
    ).read_description()
    assert "[!warning]" in desc
    assert "no longer exists on HEAD" in desc

    db.reset_engine()


# --- legitimate classify draft is unaffected ---


def test_module_curator_legitimate_classify_unaffected(tmp_path):
    """A `Classify <file>:` draft for a path that exists on HEAD and is
    NOT yet in docs/modules.yaml is filed unchanged (no advisory)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # File exists on disk; modules.yaml does NOT cover it.
    newfile = tmp_path / "src" / "robotsix_mill" / "newthing.py"
    newfile.parent.mkdir(parents=True, exist_ok=True)
    newfile.write_text("# new\n", encoding="utf-8")
    _write_modules_yaml(tmp_path, [{"id": "config", "paths": ["src/config.py"]}])

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Classify src/robotsix_mill/newthing.py: assign to a module"],
        draft_bodies=["The file src/robotsix_mill/newthing.py is unclassified."],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    tickets = service.list()
    assert len(tickets) == 2  # 1 draft + 1 rollup EPIC
    draft_tickets = [t for t in tickets if t.kind == TicketKind.TASK]
    assert len(draft_tickets) == 1
    desc = Workspace(
        settings.workspaces_dir_for("test-board"), draft_tickets[0].id
    ).read_description()
    assert "[!warning]" not in desc

    db.reset_engine()


# --- advisory: already-classified path ---


def test_module_curator_premise_check_already_classified(tmp_path):
    """A classify draft for a path already covered by a module glob in
    docs/modules.yaml returns an advisory verdict."""
    newfile = tmp_path / "src" / "robotsix_mill" / "agents" / "coding.py"
    newfile.parent.mkdir(parents=True, exist_ok=True)
    newfile.write_text("# code\n", encoding="utf-8")
    _write_modules_yaml(
        tmp_path,
        [{"id": "agents", "paths": ["src/robotsix_mill/agents/*.py"]}],
    )

    verdict = _module_curator_premise_check(
        tmp_path,
        "Classify src/robotsix_mill/agents/coding.py: assign to a module",
        "src/robotsix_mill/agents/coding.py is unclassified.",
    )
    assert verdict is not None
    disposition, note = verdict
    assert disposition == "advisory"
    assert "already classified under module agents" in note


# --- conservative: unmatched / malformed titles return None ---


def test_module_curator_premise_check_unmatched_title_returns_none(tmp_path):
    """A title that matches none of the curator shapes returns None."""
    assert (
        _module_curator_premise_check(
            tmp_path, "Some unrelated free-form title", "body"
        )
        is None
    )


def test_module_curator_premise_check_no_path_returns_none(tmp_path):
    """A create/missing title with no extractable path returns None."""
    assert (
        _module_curator_premise_check(
            tmp_path,
            "Create the missing taxonomy file (CRITICAL)",
            "the taxonomy is missing",
        )
        is None
    )


def test_module_curator_premise_check_malformed_modules_yaml(tmp_path):
    """A malformed docs/modules.yaml never blocks: the helper returns
    None for an existing classify path when YAML parsing fails."""
    newfile = tmp_path / "src" / "robotsix_mill" / "thing.py"
    newfile.parent.mkdir(parents=True, exist_ok=True)
    newfile.write_text("# x\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "modules.yaml").write_text(": : not valid yaml : [", encoding="utf-8")

    verdict = _module_curator_premise_check(
        tmp_path,
        "Classify src/robotsix_mill/thing.py: assign to a module",
        "src/robotsix_mill/thing.py is unclassified.",
    )
    assert verdict is None


# --- guard only fires for MODULE_CURATOR + repo_dir set ---


def test_module_curator_guard_not_fired_for_other_sources(tmp_path):
    """A non-MODULE_CURATOR source with the same 'missing file' title is
    never suppressed, even when the file exists."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    _write_modules_yaml(tmp_path, [{"id": "config", "paths": ["src/config.py"]}])

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Create docs/modules.yaml: missing"],
        draft_bodies=["docs/modules.yaml does not exist"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    assert len(result.drafts_created) == 1
    assert len(service.list()) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


def test_module_curator_guard_not_fired_without_repo_dir(tmp_path):
    """With repo_dir=None the guard short-circuits — the draft is filed."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="mem",
        draft_titles=["Create docs/modules.yaml: missing"],
        draft_bodies=["docs/modules.yaml does not exist"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.MODULE_CURATOR,
        service=service,
        settings=settings,
        # repo_dir not passed -> None
    )

    assert len(result.drafts_created) == 1
    assert len(service.list()) == 2  # 1 draft + 1 rollup EPIC

    db.reset_engine()


# --- _SCANNER_SOURCES vs _ROLLUP_SOURCES -----------------------------------
#
# Two features landed as concurrent PRs and both named their set
# _SCANNER_SOURCES, so the second silently shadowed the first and epic
# parenting quietly shrank from 19 sources to 5.


def test_scanner_and_rollup_source_sets_are_distinct():
    from robotsix_mill.agents.runners.pass_runner import (
        _ROLLUP_SOURCES,
        _SCANNER_SOURCES,
    )
    from robotsix_mill.core.models import SourceKind

    # Epic parenting covers every scanner source...
    assert SourceKind.AUDIT in _SCANNER_SOURCES
    assert SourceKind.TRACE_HEALTH in _SCANNER_SOURCES
    # ...while collapsing N findings into one ticket is opt-in and narrower.
    assert _ROLLUP_SOURCES < _SCANNER_SOURCES
    assert SourceKind.AUDIT not in _ROLLUP_SOURCES
    assert SourceKind.DOCSTRING_COVERAGE in _ROLLUP_SOURCES


# --- scanner rollup positive test ---


def test_scanner_rollup_collapses_multiple_drafts_to_one(tmp_path):
    """When ``scanner_rollup`` is enabled (default) and a scanner source
    produces ≥2 findings, the runner collapses them into a single rollup
    ticket with title '{source} scan: N findings (YYYY-MM-DD)'.  Every
    finding body is in the ticket description."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("memory", encoding="utf-8")

    agent_fn = _make_agent(
        updated_memory="memory",
        draft_titles=["Missing test A", "Missing test B", "Missing test C"],
        draft_bodies=["Body A details.", "Body B details.", "Body C details."],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.HEALTH,
        service=service,
        settings=settings,
    )

    # Only 1 draft created (the rollup).
    assert len(result.drafts_created) == 1
    rollup = result.drafts_created[0]
    assert "scan:" in rollup["title"]
    assert "3 findings" in rollup["title"]

    # Verify DB: 1 rollup task + 1 rollup EPIC = 2 tickets.
    tickets = service.list()
    assert len(tickets) == 2
    draft_tickets = [t for t in tickets if t.kind == TicketKind.TASK]
    assert len(draft_tickets) == 1
    draft = draft_tickets[0]
    assert "scan:" in draft.title
    assert "3 findings" in draft.title
    # Body (workspace on-disk description) contains all three findings.
    body = Workspace(
        settings.workspaces_dir_for("test-board"), draft.id
    ).read_description()
    assert "Missing test A" in body
    assert "Missing test B" in body
    assert "Missing test C" in body
    assert "Body A details." in body
    assert "Body B details." in body
    assert "Body C details." in body

    db.reset_engine()


# --- same-board gap-id dedup regression (#2688) ---


def test_same_board_gap_id_dedup(tmp_path, monkeypatch):
    """_create_one_draft deduplicates by gap_id on the same board.

    Before #2688 the gap-id check only ran in the cross-board
    (target_repo_id) branch.  A draft filed onto the pass's own
    board bypassed the check entirely, causing the same findings
    to be re-filed on every run.
    """
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    source_label = "bespoke:security-posture-audit"
    title = "llm-top-10-v3-currency gap"
    body = "Body text"
    gap_id = "llm-top-10-v3-currency"

    res = _FakeAgentResult(
        updated_memory="mem",
        draft_titles=[title],
        draft_bodies=[body],
        gap_ids=[gap_id],
    )

    # First call: creates a draft ticket.
    ticket_info, verified_gap_id = _create_one_draft(
        i=0,
        res=res,
        gap_ids=[gap_id],
        draft_target_repo_ids=[""],  # no target_repo_id → same board
        source_label=source_label,
        service=service,
        settings=settings,
        origin_session="test-session",
        repo_dir=None,
    )
    assert ticket_info is not None
    assert ticket_info["title"] == title
    assert verified_gap_id == gap_id

    # Verify exactly one ticket exists in the DB.
    tickets = service.list()
    assert len(tickets) == 1

    # Second call with the same parameters: must be deduped.
    ticket_info2, verified_gap_id2 = _create_one_draft(
        i=0,
        res=res,
        gap_ids=[gap_id],
        draft_target_repo_ids=[""],
        source_label=source_label,
        service=service,
        settings=settings,
        origin_session="test-session",
        repo_dir=None,
    )
    assert ticket_info2 is None, "second call must be deduped"
    assert verified_gap_id2 == ""

    # Still only one ticket in the DB.
    tickets = service.list()
    assert len(tickets) == 1

    db.reset_engine()


def test_canonical_gap_id_ignores_token_order():
    """Gap ids differing only in token order dedup to one key."""
    assert canonical_gap_id("ci_dart_format_gate") == "ci_dart_format_gate"
    assert canonical_gap_id("dart_format_ci_gate") == canonical_gap_id(
        "ci_dart_format_gate"
    )
    assert canonical_gap_id("enforce-dart-format-CI-gate") == canonical_gap_id(
        "add_dart_format_ci_gate_check"
    )
    assert canonical_gap_id("dart_format_ci_gate") != canonical_gap_id(
        "dart_analyze_ci_gate"
    )
    # All-stopword ids keep a non-empty key.
    assert canonical_gap_id("add_check") == "add_check"


def test_same_board_gap_id_dedup_token_order(tmp_path):
    """An open audit ticket under one token ordering suppresses a new
    draft whose gap_id is the same tokens in another order."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def _draft(title: str, gap_id: str):
        res = _FakeAgentResult(
            updated_memory="mem",
            draft_titles=[title],
            draft_bodies=["Add dart format check to the analyze job."],
            gap_ids=[gap_id],
        )
        return _create_one_draft(
            i=0,
            res=res,
            gap_ids=[gap_id],
            draft_target_repo_ids=[""],
            source_label=SourceKind.AUDIT,
            service=service,
            settings=settings,
            origin_session="test-session",
            repo_dir=None,
        )

    info, verified = _draft("Enforce dart formatting in CI", "ci_dart_format_gate")
    assert info is not None
    assert verified == "ci_dart_format_gate"

    info2, verified2 = _draft(
        "Add dart format check to CI analyze job", "dart_format_ci_gate"
    )
    assert info2 is None, "reordered gap_id must be deduped"
    assert verified2 == ""
    tasks = [t for t in service.list() if t.kind == TicketKind.TASK]
    assert len(tasks) == 1

    db.reset_engine()


def test_same_board_title_fingerprint_dedup_no_gap_id(tmp_path, monkeypatch):
    """_create_one_draft deduplicates by normalized-title fingerprint
    when no gap_id is available (same board)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    memory_file = tmp_path / "memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    source_label = "bespoke:security-posture-audit"
    title = "Some recurring finding"
    body = "Body text"

    res = _FakeAgentResult(
        updated_memory="mem",
        draft_titles=[title],
        draft_bodies=[body],
        # No gap_ids → triggers fingerprint fallback
    )

    # First call: creates a draft ticket.
    ticket_info, verified_gap_id = _create_one_draft(
        i=0,
        res=res,
        gap_ids=[],
        draft_target_repo_ids=[""],
        source_label=source_label,
        service=service,
        settings=settings,
        origin_session="test-session",
        repo_dir=None,
    )
    assert ticket_info is not None
    assert ticket_info["title"] == title
    assert verified_gap_id == ""

    tickets = service.list()
    assert len(tickets) == 1

    # Second call with the same title and no gap_id: fingerprint
    # match must find the existing ticket.
    ticket_info2, verified_gap_id2 = _create_one_draft(
        i=0,
        res=res,
        gap_ids=[],
        draft_target_repo_ids=[""],
        source_label=source_label,
        service=service,
        settings=settings,
        origin_session="test-session",
        repo_dir=None,
    )
    assert ticket_info2 is None, "second call must be deduped by fingerprint"
    assert verified_gap_id2 == ""

    # Still only one ticket in the DB.
    tickets = service.list()
    assert len(tickets) == 1

    db.reset_engine()


# ------------------------------------------------------------------ policy-addition classification


def test_is_policy_addition_detects_ci_tooling_proposals():
    """The observed recurring CI/standards-gate escalations classify as
    policy additions."""
    cases = [
        ("Add cyclomatic complexity analysis with radon/xenon", "body"),
        ("Add visual regression testing with Chromatic", "body"),
        ("Add a secret scanner (gitleaks) to pre-commit", "body"),
        ("Add an OpenSSF Scorecard workflow for container repos", "body"),
        ("Standardize code coverage measurement across the fleet", "body"),
        ("title", "Introduce a new CI workflow enforcing Conventional Commits."),
        ("title", "Propose a quality gate that fails on coverage regressions."),
    ]
    for title, body in cases:
        assert is_policy_addition(title, body), f"should classify: {title!r}"


def test_is_policy_addition_ignores_routine_code_quality():
    """Ordinary code-quality / refactor drafts are NOT policy additions."""
    cases = [
        ("Add unit tests for agents/coding.py", "Cover the untested branch."),
        ("Refactor the pass runner to reduce complexity", "Extract a helper."),
        ("Fix a typo in the README", "s/teh/the/"),
        ("Classify src/robotsix_mill/newthing.py: assign to a module", "body"),
    ]
    for title, body in cases:
        assert not is_policy_addition(title, body), f"should NOT classify: {title!r}"


def test_policy_addition_draft_is_labelled(tmp_path):
    """A survey CI-tooling proposal carries the policy-addition label in
    its ticket data (AC1)."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    title = "Add a secret scanner (gitleaks) to pre-commit"
    body = "Introduce a gitleaks pre-commit hook and a CI workflow."
    res = _FakeAgentResult(
        updated_memory="mem",
        draft_titles=[title],
        draft_bodies=[body],
    )

    ticket_info, _ = _create_one_draft(
        i=0,
        res=res,
        gap_ids=[],
        draft_target_repo_ids=[""],
        source_label=SourceKind.SURVEY,
        service=service,
        settings=settings,
        origin_session="test-session",
        repo_dir=None,
    )
    assert ticket_info is not None
    ticket = service.get(ticket_info["id"])
    assert _parse_labels(ticket.labels) == [_POLICY_ADDITION_LABEL]

    db.reset_engine()


def test_non_policy_addition_draft_is_not_labelled(tmp_path):
    """A routine audit code-quality draft is not tagged policy-addition."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    res = _FakeAgentResult(
        updated_memory="mem",
        draft_titles=["Add unit tests for agents/coding.py"],
        draft_bodies=["Cover the untested error branch."],
    )

    ticket_info, _ = _create_one_draft(
        i=0,
        res=res,
        gap_ids=[],
        draft_target_repo_ids=[""],
        source_label=SourceKind.AUDIT,
        service=service,
        settings=settings,
        origin_session="test-session",
        repo_dir=None,
    )
    assert ticket_info is not None
    ticket = service.get(ticket_info["id"])
    assert _parse_labels(ticket.labels) == []

    db.reset_engine()
