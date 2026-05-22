"""Guard tests: agent prompts must NOT duplicate tool signatures that
pydantic-ai auto-injects, and MUST contain orchestration guidance.
"""

import re

from robotsix_mill.agents import (
    refining,
    coordinating,
    health,
    auditing,
    retrospecting,
    agent_check,
)


# --- Tool-signature patterns that must NOT appear in prompts ---

# Any line that looks like a tool-signature description e.g.
#   `explore(question)` — a fast scout: it returns the paths/...
#   `read_file`/`list_dir` — read exactly the files explore pointed...
#   `edit_file(path, old_string, new_string)` — replace a unique...
#   `write_file` — create a new file...
#   `web_research(query)` — anything not in the repo.
#   `run_tests()` — runs the suite...
#   `trace_inspect(trace_id)` — ...
_SIGNATURE_PATTERNS = [
    r"`explore\(question\)`",
    r"`read_file`/`list_dir`\s*[—–-]\s*read\b",
    r"`edit_file\(path,\s*old_string,\s*new_string\)`",
    r"`write_file`\s*[—–-]\s*create\s+a\s+new\s+file",
    r"`web_research\(query\)`",
    r"`run_tests\(\)`\s*[—–-]\s*runs\s+the\s+suite",
    r"`trace_inspect\(trace_id\)`",
    r"a scout returning\s+(concise\s+)?paths.*line.ranges",
]


def _assert_no_tool_signatures(prompt: str, agent_name: str):
    """Fail if the prompt contains a redundant tool-signature description."""
    for pat in _SIGNATURE_PATTERNS:
        m = re.search(pat, prompt, re.IGNORECASE)
        assert m is None, (
            f"{agent_name} prompt contains redundant tool-signature "
            f"pattern: {pat!r}\n"
            f"Match: {m.group()!r}"
        )


# --- Per-agent tests ---


def test_refining_prompt_no_tool_signatures():
    """refining SYSTEM_PROMPT must not restate tool signatures."""
    _assert_no_tool_signatures(refining.SYSTEM_PROMPT, "refining")
    # Must contain orchestration guidance.
    p = refining.SYSTEM_PROMPT.lower()
    assert "ground the spec in the actual codebase" in p
    assert "do not web-fetch" in p or "do NOT web-fetch" in p
    assert "`web_research` only" in p or "use `web_research`" in p


def test_coordinating_prompt_no_tool_signatures():
    """coordinating _SYSTEM_PROMPT must not restate tool signatures."""
    _assert_no_tool_signatures(coordinating._SYSTEM_PROMPT, "coordinating")
    # Must contain orchestration guidance.
    p = coordinating._SYSTEM_PROMPT.lower()
    assert "prefer `explore`" in p
    assert "`edit_file`" in p
    assert "`write_file`" in p
    # The coordinator no longer runs the suite itself — the stage owns
    # the test→retry→escalate loop. The prompt must say so.
    assert "test suite" in p
    assert "<test_failure>" in p


def test_health_prompt_no_tool_signatures():
    """health SYSTEM_PROMPT must not restate tool signatures."""
    _assert_no_tool_signatures(health.SYSTEM_PROMPT, "health")
    # Must contain orchestration guidance.
    p = health.SYSTEM_PROMPT.lower()
    assert "use `list_dir`" in p
    assert "use `explore`" in p
    assert "use `web_research`" in p
    assert "module size" in p  # dimension coverage
    assert "test-suite organization" in p  # dimension 7
    assert "documentation structure" in p  # dimension 8


def test_auditing_prompt_no_tool_signatures():
    """auditing SYSTEM_PROMPT must not restate tool signatures."""
    _assert_no_tool_signatures(auditing.SYSTEM_PROMPT, "auditing")
    # Must contain orchestration guidance.
    p = auditing.SYSTEM_PROMPT.lower()
    assert "`list_dir`" in p
    assert "`explore`" in p
    assert "`web_research` is for external" in p


def test_retrospecting_deep_analysis_no_tool_signatures():
    """retrospecting _DEEP_ANALYSIS_ADDENDUM must not restate tool
    signatures."""
    _assert_no_tool_signatures(
        retrospecting._DEEP_ANALYSIS_ADDENDUM, "retrospecting"
    )
    # Must contain orchestration: you MUST call it for every trace.
    p = retrospecting._DEEP_ANALYSIS_ADDENDUM.lower()
    assert "must call `trace_inspect`" in p
    assert "every trace" in p


def test_agent_check_prompt_no_tool_signatures():
    """agent_check SYSTEM_PROMPT must contain pydantic-ai auto-injection
    awareness.  We skip the full signature-pattern check for agent_check
    because its prompt legitimately discusses tool signatures as examples
    in its analysis methodology (e.g. Dimension E examples)."""
    p = agent_check.SYSTEM_PROMPT
    # The prompt should contain pydantic-ai auto-injection awareness.
    assert "pydantic-ai" in p.lower() or "auto-inject" in p.lower()
    # The prompt should mention the tools it uses for its own job.
    assert "`explore`" in p
    assert "`read_file`" in p
    assert "`list_dir`" in p
