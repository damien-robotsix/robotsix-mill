"""Unit tests for the prerequisite gate module.

Covers ``parse_prerequisites`` (regex extraction) and
``run_prerequisite_check`` (directive verification logic) independently
from the implement-stage integration.  The subprocess seam is stubbed
via the ``runner`` parameter so tests never depend on the real
installed-package state.
"""

from pathlib import Path

from robotsix_mill.agents.prerequisite import (
    parse_prerequisites,
    run_prerequisite_check,
)


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
    spec = (
        "## Prerequisites\n```prereq\n"
        "import foo.bar\n"
        "symbol Baz from foo.qux\n"
        "```\n"
    )
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
    spec = (
        "## Prerequisites\n"
        "```bash\n"
        "symbol CostLogSource from robotsix_llmio\n"
        "```\n"
    )
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
    spec = "## Prerequisites\n```prereq\nsymbol CostLogSource from robotsix_llmio\n```\n"
    result = run_prerequisite_check(spec, Path("/tmp"), runner=_runner_all_pass)
    assert result["unmet"] == []


def test_check_unmet_directive_reported():
    """A failing import surfaces the directive in unmet."""
    spec = "## Prerequisites\n```prereq\nsymbol CostLogSource from robotsix_llmio\n```\n"
    result = run_prerequisite_check(spec, Path("/tmp"), runner=_runner_all_fail)
    assert result["unmet"] == ["symbol CostLogSource from robotsix_llmio"]
    assert "CostLogSource" in result["reason"]


def test_check_partial_unmet():
    """Only the failing directives are reported."""
    spec = (
        "## Prerequisites\n```prereq\n"
        "import foo.bar\n"
        "symbol Baz from foo.qux\n"
        "```\n"
    )

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
