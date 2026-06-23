"""Guard tests: agent prompts must NOT duplicate tool signatures that
pydantic-ai auto-injects, and MUST contain orchestration guidance.
"""

import re
from pathlib import Path

import pytest

from robotsix_mill.agents import (
    refining,
    health,
    auditing,
    agent_check,
)
from robotsix_mill.agents.yaml_loader import load_agent_definition


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
    assert "`ask_web_knowledge` only" in p or "use `ask_web_knowledge`" in p


def test_coordinating_prompt_no_tool_signatures():
    """coordinating system prompt must not restate tool signatures."""
    definition = load_agent_definition(
        Path(__file__).parent.parent.parent / "agent_definitions" / "implement.yaml"
    )
    prompt = definition.system_prompt
    _assert_no_tool_signatures(prompt, "coordinating")
    # Must contain orchestration guidance.
    p = prompt.lower()
    assert "prefer `explore`" in p
    assert "`edit_file`" in p
    assert "`write_file`" in p
    # The coordinator no longer runs the suite itself — the stage owns
    # the test→retry→escalate loop. The prompt must say so.
    assert "test suite" in p
    assert "test-failure" in p
    # Scope guardrails: the prompt must forbid scope creep.
    assert "do not delete" in p
    assert "do not rename" in p
    assert "do not remove" in p
    assert "do not refactor" in p
    assert "reformat" in p
    assert "part of the code" in p
    assert "out of scope" in p


def test_document_prompt_requires_applied_edits_for_user_facing():
    """The document agent prompt must forbid recommendation-only doc
    deliverables: for a user-facing change the agent MUST apply the edit
    via edit_file/write_file, not merely recommend one. It must also no
    longer tell the agent to return the DocResult immediately after merely
    locating docs for a user-facing change."""
    definition = load_agent_definition(
        Path(__file__).parent.parent.parent / "agent_definitions" / "document.yaml"
    )
    prompt = definition.system_prompt
    low = prompt.lower()

    # Must require the actual applied edit for user-facing changes.
    assert "invalid deliverable" in low
    assert "edit_file" in prompt and "write_file" in prompt
    assert "recommend" in low  # recommendation-only is called out as invalid

    # Must reserve budget for editing rather than blowing it on exploration.
    assert "reserve" in low and "budget" in low

    # Must NOT instruct an immediate DocResult return after merely locating
    # docs for a user-facing change. The old guidance said to "stop and
    # return the `DocResult` immediately" once it knew which docs to update.
    assert "which\n    docs need updating, stop and return" not in low
    assert "stop and return the `docresult` immediately" not in low
    # Any early-return guidance must be scoped to the internal-only path.
    assert "internal-only" in low


def test_health_prompt_no_tool_signatures():
    """health SYSTEM_PROMPT must not restate tool signatures."""
    _assert_no_tool_signatures(health.SYSTEM_PROMPT, "health")
    # Must contain orchestration guidance.
    p = health.SYSTEM_PROMPT.lower()
    assert "use `list_dir`" in p
    assert "use `explore`" in p
    # health has NO web tool — inspection is local-clone only. The prompt
    # must not steer the agent toward web_research (web_knowledge: false).
    assert "web_research" not in p
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
    assert "`ask_web_knowledge` is for external" in p


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


# --- Per-ticket memory write patterns that must NOT appear ---

# Patterns that positively encourage or permit recording ticket-specific
# information in the agent memory ledger.  These are prohibited because
# ticket history now lives in the DB and is surfaced via
# `<recent_proposals>`; memory is for general observations and patterns
# only.
#
# Each pattern is a (regex, description) pair.  A match fails the test
# UNLESS the matching line is inside a "DO NOT" / "do not" / "NEVER" /
# "FORBIDDEN" / "PROHIBITED" negation context (the MEMORY USAGE section
# legitimately lists prohibited behaviours).

_PER_TICKET_MEMORY_PATTERNS = [
    (r"record[ \t]+(the[ \t]+)?ticket[ \t]+IDs?\b", "record the ticket ID(s)"),
    (
        r"note[ \t]+(down[ \t]+)?(the[ \t]+)?ticket[ \t]+IDs?\b",
        "note down the ticket ID(s)",
    ),
    (
        r"keep[ \t]+(a[ \t]+)?(log|diary|record)[ \t]+of[ \t]+tickets?\b",
        "keep a log/diary of tickets",
    ),
    (r"per-ticket[ \t]+(notes?|diary|log|record)\b", "per-ticket notes/diary"),
    (r"per-proposal[ \t]+(notes?|diary|log|record)\b", "per-proposal notes/diary"),
    (r"per-finding[ \t]+(notes?|diary|log|record)\b", "per-finding notes/diary"),
    (
        r"track[ \t]+what[ \t]+you[ \t]+(proposed|filed|submitted)",
        "track what you proposed",
    ),
    (
        r"record[ \t]+each[ \t]+(finding|proposal|ticket|gap)",
        "record each finding/proposal",
    ),
    (
        r"diary[ \t]+of[ \t]+(tickets?|proposals?|findings?)",
        "diary of tickets/proposals",
    ),
    (
        r"log[ \t]+(each|every)[ \t]+(ticket|proposal|finding)",
        "log each ticket/proposal",
    ),
    (
        r"maintain[ \t]+a[ \t]+(list|log)[ \t]+of[ \t]+(tickets?|proposals?)",
        "maintain a list of tickets",
    ),
    (
        r"write[ \t]+(down[ \t]+)?(the[ \t]+)?ticket[ \t]+(ID|number|reference)",
        "write down the ticket ID",
    ),
    (r"store[ \t]+(the[ \t]+)?ticket[ \t]+(ID|number)", "store the ticket ID"),
    (
        r"capture[ \t]+(the[ \t]+)?ticket[ \t]+(ID|number|state)",
        "capture the ticket ID",
    ),
]


def _has_negation_context(text: str, match_start: int) -> bool:
    """Check whether the match at ``match_start`` appears inside a
    negation / prohibition context (e.g. 'DO NOT …')."""
    # Look backwards up to 200 chars for negation markers across lines.
    before = text[max(0, match_start - 200) : match_start]
    return bool(
        re.search(
            r"(?i)(?:DO\s+NOT|NEVER|FORBIDDEN|PROHIBITED|MUST\s+NOT|SHOULD\s+NOT|"
            r"\bNOT\s+a\b|is\s+NOT\b|is\s+not\b|are\s+NOT\b|are\s+not\b)",
            before,
        )
    )


@pytest.mark.parametrize(
    "agent_name,yaml_path",
    [
        ("audit", "periodic/audit.yaml"),
        ("health", "periodic/health.yaml"),
        ("test_gap", "periodic/test_gap.yaml"),
        ("agent_check", "periodic/agent_check.yaml"),
        ("survey", "periodic/survey.yaml"),
        ("retrospect", "retrospect.yaml"),
    ],
)
def test_periodic_prompts_prohibit_per_ticket_memory_writes(
    agent_name: str, yaml_path: str
):
    """No periodic agent prompt may encourage or permit per-ticket
    memory writes."""
    definition = load_agent_definition(
        Path(__file__).parent.parent.parent / "agent_definitions" / yaml_path
    )
    prompt = definition.system_prompt

    for pattern, desc in _PER_TICKET_MEMORY_PATTERNS:
        for m in re.finditer(pattern, prompt, re.IGNORECASE):
            if not _has_negation_context(prompt, m.start()):
                pytest.fail(
                    f"{agent_name} prompt contains prohibited "
                    f"per-ticket-memory language: {desc!r}\n"
                    f"Match: {m.group()!r}\n"
                    f"Context: …{prompt[max(0, m.start() - 40) : m.end() + 40]}…"
                )


_SHARED_ACTION_TAG_MARKER = "resolve a GitHub Action tag to its commit SHA"


@pytest.mark.parametrize(
    "agent_name,yaml_path",
    [
        ("ci_fix", "ci_fix.yaml"),
        ("implement", "implement.yaml"),
        ("refine", "refine.yaml"),
    ],
)
def test_action_tag_resolution_instruction_present(
    agent_name: str, yaml_path: str
) -> None:
    """All three pipeline agent prompts must contain the action-tag
    resolution instruction with the shared marker phrase."""
    definition = load_agent_definition(
        Path(__file__).parent.parent.parent / "agent_definitions" / yaml_path
    )
    prompt = definition.system_prompt
    assert _SHARED_ACTION_TAG_MARKER in prompt, (
        f"{agent_name} prompt ({yaml_path}) is missing the shared "
        f"marker phrase: {_SHARED_ACTION_TAG_MARKER!r}"
    )
