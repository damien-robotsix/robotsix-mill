"""Unit tests for ``robotsix_mill.agents.workflow_portability``."""

from __future__ import annotations

from robotsix_mill.agents import workflow_portability as wp

# -- kind_for ---------------------------------------------------------------


def test_kind_for_llm_agent():
    assert wp.kind_for("audit") == "llm_agent"
    assert wp.kind_for("bc_check") == "llm_agent"
    assert wp.kind_for("completeness_check") == "llm_agent"
    assert wp.kind_for("copy_paste") == "llm_agent"
    assert wp.kind_for("survey") == "llm_agent"
    assert wp.kind_for("test_gap") == "llm_agent"
    assert wp.kind_for("docstring_coverage") == "llm_agent"
    assert wp.kind_for("module_curator") == "llm_agent"
    assert wp.kind_for("module_size") == "llm_agent"
    assert wp.kind_for("mypy_baseline") == "llm_agent"
    assert wp.kind_for("forge_parity") == "llm_agent"
    assert wp.kind_for("triage_boilerplate") == "llm_agent"
    assert wp.kind_for("repo_description_sync") == "llm_agent"
    assert wp.kind_for("health") == "llm_agent"
    assert wp.kind_for("agent_check") == "llm_agent"


def test_kind_for_schedule_only():
    assert wp.kind_for("diagnostic") == "schedule_only"
    assert wp.kind_for("trace_review") == "schedule_only"
    assert wp.kind_for("config_sync") == "schedule_only"
    assert wp.kind_for("credit_balance") == "schedule_only"
    assert wp.kind_for("member_sync") == "schedule_only"
    assert wp.kind_for("data_dir_gc") == "schedule_only"
    assert wp.kind_for("pin_bump") == "schedule_only"
    assert wp.kind_for("roadmap_sync") == "schedule_only"


def test_kind_for_global_only():
    assert wp.kind_for("langfuse_cleanup") == "global_only"
    assert wp.kind_for("meta") == "global_only"
    assert wp.kind_for("run_health") == "global_only"
    assert wp.kind_for("timeout_escalation") == "global_only"
    assert wp.kind_for("trace_health") == "global_only"


def test_kind_for_mill_only():
    assert wp.kind_for("state_sync") == "mill_only"
    assert wp.kind_for("frontend_sync") == "mill_only"


def test_kind_for_bespoke_unknown_name():
    assert wp.kind_for("my-custom-thing") == "bespoke"
    assert wp.kind_for("nonexistent_workflow") == "bespoke"
    assert wp.kind_for("") == "bespoke"


# -- is_portable ------------------------------------------------------------


def test_is_portable_true():
    assert wp.is_portable("audit") is True
    assert wp.is_portable("diagnostic") is True
    assert wp.is_portable("trace_review") is True


def test_is_portable_false():
    assert wp.is_portable("langfuse_cleanup") is False
    assert wp.is_portable("meta") is False
    assert wp.is_portable("state_sync") is False
    assert wp.is_portable("frontend_sync") is False
    assert wp.is_portable("my-bespoke-agent") is False


# -- render_workflow_portability --------------------------------------------


def test_render_contains_expected_headers():
    output = wp.render_workflow_portability()
    assert "## Workflow Portability" in output
    assert "| Workflow | Portability | Notes |" in output
    assert "|----------|-------------|-------|" in output


def test_render_internal_sorted_before_portable():
    """Internal (non-portable) workflows must appear before portable ones."""
    output = wp.render_workflow_portability()
    lines = output.split("\n")
    # Find the first row after the header/separator
    data_rows = [
        line for line in lines if line.startswith("| `") and "Workflow" not in line
    ]
    assert len(data_rows) > 0

    # The first row after header should be internal
    first_label = data_rows[0]
    assert "**internal**" in first_label, (
        f"First row should be internal, got: {first_label!r}"
    )

    # Find the transition point: all rows before the first portable should be internal
    saw_portable = False
    for row in data_rows:
        if saw_portable:
            assert "**internal**" not in row, f"Internal row after portable: {row!r}"
        if "**portable**" in row:
            saw_portable = True


def test_render_mill_only_label_and_note():
    output = wp.render_workflow_portability()
    assert (
        "| `state_sync` | **internal** | Mill-only: hardcoded to robotsix-mill source paths |"
        in output
    )
    assert (
        "| `frontend_sync` | **internal** | Mill-only: hardcoded to robotsix-mill source paths |"
        in output
    )


def test_render_global_only_label_and_note():
    output = wp.render_workflow_portability()
    assert (
        "| `langfuse_cleanup` | **internal** | Cross-repo infra, not per-repo presence-managed |"
        in output
    )
    assert (
        "| `meta` | **internal** | Cross-repo infra, not per-repo presence-managed |"
        in output
    )
    assert (
        "| `run_health` | **internal** | Cross-repo infra, not per-repo presence-managed |"
        in output
    )
    assert (
        "| `timeout_escalation` | **internal** | Cross-repo infra, not per-repo presence-managed |"
        in output
    )
    assert (
        "| `trace_health` | **internal** | Cross-repo infra, not per-repo presence-managed |"
        in output
    )


def test_render_schedule_only_label_and_note():
    output = wp.render_workflow_portability()
    assert "| `diagnostic` | **portable** | Deterministic schedule task |" in output


def test_render_llm_agent_label_and_note():
    output = wp.render_workflow_portability()
    assert (
        "| `audit` | **portable** | LLM periodic agent, enable-able on any repo |"
        in output
    )
    assert (
        "| `bc_check` | **portable** | LLM periodic agent, enable-able on any repo |"
        in output
    )


def test_render_no_bespoke_workflows():
    """Bespoke workflows are not in _BUILTIN_KINDS so they must not appear."""
    output = wp.render_workflow_portability()
    assert "bespoke" not in output
