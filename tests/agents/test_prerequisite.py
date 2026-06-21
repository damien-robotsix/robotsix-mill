"""Unit tests for the prerequisite gate module.

Covers ``parse_prerequisites`` (regex extraction) and
``run_prerequisite_check`` (directive verification logic) independently
from the implement-stage integration.  The subprocess seam is stubbed
via the ``runner`` parameter so tests never depend on the real
installed-package state.
"""

from pathlib import Path

from robotsix_mill.agents import prerequisite
from robotsix_mill.agents.prerequisite import (
    _build_batch_script,
    _extract_dependency_diffs,
    _sandbox_batch_check,
    parse_prerequisites,
    run_prerequisite_check,
)
from robotsix_mill.sandbox import SandboxError


# --- parse_prerequisites ---


def test_parse_symbol_directive():
    """A ``symbol X from mod`` directive in a prereq block is extracted."""
    spec = (
        "## Problem\nstuff\n\n"
        "## Prerequisites\n"
        "```prereq\n"
        "symbol CostLogSource from robotsix_llmio\n"
        "```\n"
    )
    directives = parse_prerequisites(spec)
    assert len(directives) == 1
    assert directives[0]["directive"] == "symbol CostLogSource from robotsix_llmio"
    assert directives[0]["code"] == "from robotsix_llmio import CostLogSource"


def test_parse_import_directive():
    """An ``import mod.path`` directive is extracted."""
    spec = "## Prerequisites\n```prereq\nimport robotsix_llmio.read\n```\n"
    directives = parse_prerequisites(spec)
    assert len(directives) == 1
    assert directives[0]["directive"] == "import robotsix_llmio.read"
    assert directives[0]["code"] == "import robotsix_llmio.read"


def test_parse_multiple_directives():
    """Multiple directive lines are all extracted, in order."""
    spec = "## Prerequisites\n```prereq\nimport foo.bar\nsymbol Baz from foo.qux\n```\n"
    directives = parse_prerequisites(spec)
    assert [d["directive"] for d in directives] == [
        "import foo.bar",
        "symbol Baz from foo.qux",
    ]


def test_parse_no_section_returns_empty():
    """A spec with no ## Prerequisites section yields no directives."""
    spec = "## Problem\nDo a thing.\n## Acceptance criteria\n- works\n"
    assert parse_prerequisites(spec) == []


def test_parse_empty_block_returns_empty():
    """An empty prereq block yields no directives."""
    spec = "## Prerequisites\n```prereq\n```\n"
    assert parse_prerequisites(spec) == []


def test_parse_ignores_prose_and_unrecognized_lines():
    """Prose lines and unrecognized directive forms inside the block
    are ignored; only valid directives survive."""
    spec = (
        "## Prerequisites\n"
        "Some prose explaining the gate.\n"
        "```prereq\n"
        "this is not a directive\n"
        "grep site-packages for something\n"
        "symbol CostLogSource from robotsix_llmio\n"
        "```\n"
    )
    directives = parse_prerequisites(spec)
    assert [d["directive"] for d in directives] == [
        "symbol CostLogSource from robotsix_llmio"
    ]


def test_parse_ignores_other_code_fences():
    """Directives in a non-``prereq`` fence are not extracted."""
    spec = "## Prerequisites\n```bash\nsymbol CostLogSource from robotsix_llmio\n```\n"
    assert parse_prerequisites(spec) == []


def test_parse_tolerates_extra_whitespace():
    """Leading/trailing whitespace around directives is tolerated."""
    spec = (
        "##   Prerequisites  \n"
        "```prereq\n"
        "   symbol   CostLogSource   from   robotsix_llmio   \n"
        "\n"
        "```\n"
    )
    directives = parse_prerequisites(spec)
    assert directives[0]["directive"] == "symbol CostLogSource from robotsix_llmio"


def test_parse_prereq_only_within_section():
    """A ```prereq fence outside the Prerequisites section is not used."""
    spec = (
        "## Prerequisites\n"
        "No block here.\n"
        "## Other section\n"
        "```prereq\n"
        "symbol CostLogSource from robotsix_llmio\n"
        "```\n"
    )
    assert parse_prerequisites(spec) == []


# --- run_prerequisite_check ---


def _runner_all_pass(code, repo_dir, timeout):
    return 0


def _runner_all_fail(code, repo_dir, timeout):
    return 1


def test_check_no_section_proceeds():
    """No ## Prerequisites section → empty unmet (proceed)."""
    result = run_prerequisite_check("## Problem\nx\n", Path("/tmp"))
    assert result["unmet"] == []


def test_check_empty_block_proceeds():
    """Empty prereq block → empty unmet (proceed)."""
    spec = "## Prerequisites\n```prereq\n```\n"
    result = run_prerequisite_check(spec, Path("/tmp"))
    assert result["unmet"] == []


def test_check_all_pass_proceeds():
    """Every directive exits zero → empty unmet."""
    spec = (
        "## Prerequisites\n```prereq\nsymbol CostLogSource from robotsix_llmio\n```\n"
    )
    result = run_prerequisite_check(spec, Path("/tmp"), runner=_runner_all_pass)
    assert result["unmet"] == []


def test_check_unmet_directive_reported():
    """A failing import surfaces the directive in unmet."""
    spec = (
        "## Prerequisites\n```prereq\nsymbol CostLogSource from robotsix_llmio\n```\n"
    )
    result = run_prerequisite_check(spec, Path("/tmp"), runner=_runner_all_fail)
    assert result["unmet"] == ["symbol CostLogSource from robotsix_llmio"]
    assert "CostLogSource" in result["reason"]


def test_check_partial_unmet():
    """Only the failing directives are reported."""
    spec = "## Prerequisites\n```prereq\nimport foo.bar\nsymbol Baz from foo.qux\n```\n"

    def runner(code, repo_dir, timeout):
        return 0 if code == "import foo.bar" else 1

    result = run_prerequisite_check(spec, Path("/tmp"), runner=runner)
    assert result["unmet"] == ["symbol Baz from foo.qux"]


def test_check_no_repo_proceeds():
    """With directives but no repo_dir, the gate proceeds (cannot verify)."""
    spec = "## Prerequisites\n```prereq\nimport robotsix_llmio\n```\n"
    result = run_prerequisite_check(spec, None, runner=_runner_all_fail)
    assert result["unmet"] == []


def test_check_runner_error_treated_as_met():
    """A runner that raises is swallowed — the directive is treated as
    met so a checker fault never blocks a ticket."""
    spec = "## Prerequisites\n```prereq\nimport robotsix_llmio\n```\n"

    def boom(code, repo_dir, timeout):
        raise RuntimeError("subprocess exploded")

    result = run_prerequisite_check(spec, Path("/tmp"), runner=boom)
    assert result["unmet"] == []


# --- _sandbox_batch_check (sandbox path) ---

_TWO_DIRECTIVES = [
    {"directive": "import foo.bar", "code": "import foo.bar"},
    {
        "directive": "symbol Baz from foo.qux",
        "code": "from foo.qux import Baz",
    },
]


class _DummySettings:
    """Stand-in for ``Settings`` — only its non-None-ness matters here;
    ``sandbox.run`` is mocked so no real attribute is read."""


def test_build_batch_script_is_valid_python():
    """The generated script compiles and contains a per-directive
    ``try/except ImportError`` block plus OK/FAIL markers."""
    script = _build_batch_script(_TWO_DIRECTIVES)
    # Compiles cleanly (stdlib-only, no top-level target imports).
    compile(script, "<batch>", "exec")
    assert "try:" in script
    assert "except ImportError:" in script
    assert "import foo.bar" in script
    assert "from foo.qux import Baz" in script
    assert "PREREQ_OK:0" in script
    assert "PREREQ_FAIL:0" in script
    assert "PREREQ_OK:1" in script
    assert "PREREQ_FAIL:1" in script


def test_sandbox_batch_all_pass(monkeypatch):
    """All directives report PREREQ_OK → empty unmet."""

    def fake_run(command, *, repo_dir, settings, install_project, sandbox_image):
        assert install_project is True
        return 0, "PREREQ_OK:0\nPREREQ_OK:1\n"

    monkeypatch.setattr(prerequisite.sandbox, "run", fake_run)
    unmet, error = _sandbox_batch_check(
        _TWO_DIRECTIVES, Path("/tmp"), _DummySettings(), None
    )
    assert unmet == []
    assert error is None


def test_sandbox_batch_one_fail(monkeypatch):
    """A PREREQ_FAIL marker surfaces that directive in unmet."""

    def fake_run(command, *, repo_dir, settings, install_project, sandbox_image):
        return 0, "PREREQ_OK:0\nPREREQ_FAIL:1\n"

    monkeypatch.setattr(prerequisite.sandbox, "run", fake_run)
    unmet, error = _sandbox_batch_check(
        _TWO_DIRECTIVES, Path("/tmp"), _DummySettings(), None
    )
    assert unmet == ["symbol Baz from foo.qux"]
    assert error is None


def test_sandbox_batch_sandbox_error(monkeypatch):
    """A SandboxError degrades gracefully → empty unmet, error string."""

    def fake_run(command, *, repo_dir, settings, install_project, sandbox_image):
        raise SandboxError("no docker")

    monkeypatch.setattr(prerequisite.sandbox, "run", fake_run)
    unmet, error = _sandbox_batch_check(
        _TWO_DIRECTIVES, Path("/tmp"), _DummySettings(), None
    )
    assert unmet == []
    assert error == "sandbox unavailable"


def test_sandbox_batch_nonzero_no_markers(monkeypatch):
    """Non-zero exit with no markers → ALL directives unmet (conservative)."""

    def fake_run(command, *, repo_dir, settings, install_project, sandbox_image):
        return 1, "Traceback (most recent call last): SyntaxError\n"

    monkeypatch.setattr(prerequisite.sandbox, "run", fake_run)
    unmet, error = _sandbox_batch_check(
        _TWO_DIRECTIVES, Path("/tmp"), _DummySettings(), None
    )
    assert unmet == ["import foo.bar", "symbol Baz from foo.qux"]
    assert error is None


def test_check_settings_takes_sandbox_path(monkeypatch):
    """``run_prerequisite_check`` with ``settings`` routes through the
    sandbox batch checker (default runner, no explicit ``runner=``)."""
    spec = (
        "## Prerequisites\n```prereq\nsymbol CostLogSource from robotsix_llmio\n```\n"
    )
    captured = {}

    def fake_batch(directives, repo_dir, settings, sandbox_image):
        captured["directives"] = directives
        captured["sandbox_image"] = sandbox_image
        return ["symbol CostLogSource from robotsix_llmio"], None

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", fake_batch)
    result = run_prerequisite_check(
        spec, Path("/tmp"), settings=_DummySettings(), sandbox_image="img:1"
    )
    assert result["unmet"] == ["symbol CostLogSource from robotsix_llmio"]
    assert captured["sandbox_image"] == "img:1"
    assert len(captured["directives"]) == 1


def test_check_settings_all_pass(monkeypatch):
    """Sandbox path reporting no unmet → gate passes."""
    spec = (
        "## Prerequisites\n```prereq\nsymbol CostLogSource from robotsix_llmio\n```\n"
    )

    def fake_batch(directives, repo_dir, settings, sandbox_image):
        return [], None

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", fake_batch)
    result = run_prerequisite_check(spec, Path("/tmp"), settings=_DummySettings())
    assert result["unmet"] == []


def test_check_explicit_runner_bypasses_sandbox(monkeypatch):
    """An explicit ``runner=`` still bypasses the sandbox entirely even
    when ``settings`` is supplied."""

    def exploding_batch(*a, **kw):  # pragma: no cover - must not be called
        raise AssertionError("sandbox path must not be taken")

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", exploding_batch)
    spec = (
        "## Prerequisites\n```prereq\nsymbol CostLogSource from robotsix_llmio\n```\n"
    )
    result = run_prerequisite_check(
        spec, Path("/tmp"), runner=_runner_all_fail, settings=_DummySettings()
    )
    assert result["unmet"] == ["symbol CostLogSource from robotsix_llmio"]


# --- same-repo skip ---


def test_same_repo_symbol_is_skipped(tmp_path):
    """A prereq whose top-level module matches the repo's own package
    is skipped (treated as met) — same-repo symbols are deliverables,
    not prerequisites."""
    # Set up a repo with a pyproject.toml naming its package.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'robotsix-auto-mail'\n", encoding="utf-8"
    )
    # Also create the src/ package for the fallback path.
    pkg = repo / "src" / "robotsix_auto_mail"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").touch()

    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "symbol get_record_by_id from robotsix_auto_mail.db\n"
        "```\n"
    )
    result = run_prerequisite_check(spec, repo, runner=_runner_all_fail)
    # Even though the runner would fail, the directive is same-repo →
    # skipped, so the gate proceeds.
    assert result["unmet"] == []


def test_same_repo_import_is_skipped(tmp_path):
    """An ``import`` directive for a same-repo module is also skipped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'robotsix-auto-mail'\n", encoding="utf-8"
    )
    spec = "## Prerequisites\n```prereq\nimport robotsix_auto_mail.db\n```\n"
    result = run_prerequisite_check(spec, repo, runner=_runner_all_fail)
    assert result["unmet"] == []


def test_mixed_same_repo_and_external_only_external_checked(tmp_path):
    """When a spec has both same-repo and external prereqs, only the
    external ones are checked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'my-project'\n", encoding="utf-8"
    )
    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "symbol get_record_by_id from my_project.db\n"
        "symbol CostLogSource from robotsix_llmio\n"
        "```\n"
    )

    def runner(code, repo_dir, timeout):
        # The external one fails.
        if "robotsix_llmio" in code:
            return 1
        return 0

    result = run_prerequisite_check(spec, repo, runner=runner)
    # Only the external prereq is reported as unmet.
    assert result["unmet"] == ["symbol CostLogSource from robotsix_llmio"]


def test_same_repo_all_skipped_proceeds(tmp_path):
    """When ALL prereqs are same-repo, the gate proceeds cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'my-project'\n", encoding="utf-8"
    )
    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "import my_project.helpers\n"
        "symbol get_record from my_project.db\n"
        "```\n"
    )
    result = run_prerequisite_check(spec, repo, runner=_runner_all_fail)
    assert result["unmet"] == []


def test_no_pyproject_does_not_skip(tmp_path):
    """When the repo has no pyproject.toml and no src/ packages, same-repo
    detection has no data → directives are checked normally (no skip)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # No pyproject.toml, no src/ — cannot determine package name.
    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "symbol get_record_by_id from robotsix_auto_mail.db\n"
        "```\n"
    )
    result = run_prerequisite_check(spec, repo, runner=_runner_all_fail)
    # Without package name info, the directive is checked normally.
    assert result["unmet"] == ["symbol get_record_by_id from robotsix_auto_mail.db"]


# --- dependency-diff re-check ---


def test_extract_dependency_diffs_pyproject():
    """A ```diff fence modifying pyproject.toml is extracted."""
    spec = (
        "## Changes\n"
        "```diff\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -10,7 +10,7 @@\n"
        "-rev = 'old'\n"
        "+rev = 'new'\n"
        "```\n"
    )
    diffs = _extract_dependency_diffs(spec)
    assert len(diffs) == 1
    assert "pyproject.toml" in diffs[0]


def test_extract_dependency_diffs_uv_lock():
    """A ```diff fence modifying uv.lock is extracted."""
    spec = "```diff\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1,1 +1,1 @@\n-old\n+new\n```\n"
    diffs = _extract_dependency_diffs(spec)
    assert len(diffs) == 1
    assert "uv.lock" in diffs[0]


def test_extract_dependency_diffs_multiple():
    """Multiple diff fences that touch pyproject.toml/uv.lock are all extracted."""
    spec = (
        "```diff\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "```\n"
        "```diff\n"
        "--- a/uv.lock\n"
        "+++ b/uv.lock\n"
        "@@ -1 +1 @@\n"
        "-c\n"
        "+d\n"
        "```\n"
    )
    diffs = _extract_dependency_diffs(spec)
    assert len(diffs) == 2


def test_extract_dependency_diffs_ignores_other_files():
    """Diff fences modifying files other than pyproject.toml/uv.lock are
    not extracted."""
    spec = (
        "```diff\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n```\n"
    )
    diffs = _extract_dependency_diffs(spec)
    assert diffs == []


def test_extract_dependency_diffs_no_diff_blocks():
    """A spec with no ```diff blocks returns an empty list."""
    spec = "## Prerequisites\n```prereq\nsymbol Foo from bar\n```\n"
    assert _extract_dependency_diffs(spec) == []


def test_recheck_applies_dep_diff_and_passes(monkeypatch, tmp_path):
    """When a sandbox check fails but the spec contains a pyproject.toml
    diff, the gate applies the diff, re-checks, and proceeds if the
    re-check passes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("old\n", encoding="utf-8")

    call_count = 0

    def fake_sandbox(directives, repo_dir, settings, sandbox_image):
        nonlocal call_count
        call_count += 1
        content = (repo_dir / "pyproject.toml").read_text(encoding="utf-8")
        if "new_sha" in content:
            # Patched — prerequisites satisfied.
            return [], None
        # Original — unmet.
        return ["symbol Foo from bar"], None

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", fake_sandbox)

    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "symbol Foo from bar\n"
        "```\n"
        "## Changes\n"
        "```diff\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new_sha\n"
        "```\n"
    )

    class _S:
        pass

    result = run_prerequisite_check(spec, repo, settings=_S(), sandbox_image="img:1")
    assert result["unmet"] == []
    assert call_count == 2
    # Verify pyproject.toml was restored.
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == "old\n"


def test_recheck_still_unmet_blocks(monkeypatch, tmp_path):
    """When the dep-diff re-check still fails, the gate blocks normally."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("old\n", encoding="utf-8")

    call_count = 0

    def fake_sandbox(directives, repo_dir, settings, sandbox_image):
        nonlocal call_count
        call_count += 1
        return ["symbol Foo from bar"], None

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", fake_sandbox)

    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "symbol Foo from bar\n"
        "```\n"
        "```diff\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new_sha\n"
        "```\n"
    )

    class _S:
        pass

    result = run_prerequisite_check(spec, repo, settings=_S(), sandbox_image="img:1")
    assert result["unmet"] == ["symbol Foo from bar"]
    assert call_count == 2
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == "old\n"


def test_recheck_no_diff_blocks_still_blocks(monkeypatch, tmp_path):
    """When unmet prereqs exist but no diff blocks are in the spec,
    the gate blocks without attempting a re-check."""
    repo = tmp_path / "repo"
    repo.mkdir()

    call_count = 0

    def fake_sandbox(directives, repo_dir, settings, sandbox_image):
        nonlocal call_count
        call_count += 1
        return ["symbol Foo from bar"], None

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", fake_sandbox)

    spec = "## Prerequisites\n```prereq\nsymbol Foo from bar\n```\n"

    class _S:
        pass

    result = run_prerequisite_check(spec, repo, settings=_S(), sandbox_image="img:1")
    assert result["unmet"] == ["symbol Foo from bar"]
    # Only one call — no re-check when no diffs.
    assert call_count == 1


def test_recheck_restores_even_on_sandbox_error(monkeypatch, tmp_path):
    """When the sandbox raises during the re-check, originals are still
    restored and the gate blocks with the original unmet list."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("old\n", encoding="utf-8")

    call_count = 0

    def fake_sandbox(directives, repo_dir, settings, sandbox_image):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ["symbol Foo from bar"], None
        raise SandboxError("no docker")

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", fake_sandbox)

    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "symbol Foo from bar\n"
        "```\n"
        "```diff\n"
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new_sha\n"
        "```\n"
    )

    class _S:
        pass

    result = run_prerequisite_check(spec, repo, settings=_S(), sandbox_image="img:1")
    # Falls back to original unmet list.
    assert result["unmet"] == ["symbol Foo from bar"]
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == "old\n"


def test_recheck_preserves_uv_lock(monkeypatch, tmp_path):
    """A diff modifying uv.lock is applied and restored correctly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("old\n", encoding="utf-8")
    (repo / "uv.lock").write_text("lock_old\n", encoding="utf-8")

    def fake_sandbox(directives, repo_dir, settings, sandbox_image):
        lock_content = (repo_dir / "uv.lock").read_text(encoding="utf-8")
        if "lock_new" in lock_content:
            return [], None
        return ["symbol Foo from bar"], None

    monkeypatch.setattr(prerequisite, "_sandbox_batch_check", fake_sandbox)

    spec = (
        "## Prerequisites\n"
        "```prereq\n"
        "symbol Foo from bar\n"
        "```\n"
        "```diff\n"
        "--- a/uv.lock\n"
        "+++ b/uv.lock\n"
        "@@ -1 +1 @@\n"
        "-lock_old\n"
        "+lock_new\n"
        "```\n"
    )

    class _S:
        pass

    result = run_prerequisite_check(spec, repo, settings=_S(), sandbox_image="img:1")
    assert result["unmet"] == []
    assert (repo / "uv.lock").read_text(encoding="utf-8") == "lock_old\n"
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == "old\n"
