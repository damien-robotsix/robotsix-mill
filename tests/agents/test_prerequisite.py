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
