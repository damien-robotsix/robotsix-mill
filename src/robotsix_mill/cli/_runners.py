"""Runner registry — maps CLI command names to their runner module/function."""

from __future__ import annotations

_RUNNERS: dict[str, dict[str, str]] = {
    "audit": {
        "module": "runners.periodic_runner",
        "function": "run_audit_pass",
        "label": "Audit pass",
        "format": "memory_drafts",
    },
    "health": {
        "module": "runners.periodic_runner",
        "function": "run_health_pass",
        "label": "Health pass",
        "format": "memory_drafts",
    },
    "agent-check": {
        "module": "runners.periodic_runner",
        "function": "run_agent_check_pass",
        "label": "Agent-check pass",
        "format": "memory_drafts",
    },
    "test-gap": {
        "module": "runners.periodic_runner",
        "function": "run_test_gap_pass",
        "label": "Test-gap pass",
        "format": "memory_drafts",
    },
    "config-sync": {
        "module": "runners.periodic_runner",
        "function": "run_config_sync_pass",
        "label": "Config-sync pass",
        "format": "memory_drafts",
    },
    "member-sync": {
        "module": "runners.member_sync_runner",
        "function": "run_member_sync_pass",
        "label": "Member-sync pass",
        "format": "member_sync",
    },
    "trace-health": {
        "module": "runners.trace_health_runner",
        "function": "run_trace_health_check",
        "label": "Trace-health check",
        "format": "trace_health",
    },
    "trace-review": {
        "module": "runners.trace_review_runner",
        "function": "run_trace_review_pass",
        "label": "Trace-review pass",
        "format": "trace_review",
    },
    "langfuse-cleanup": {
        "module": "runners.langfuse_cleanup_runner",
        "function": "run_langfuse_cleanup_pass",
        "label": "Langfuse cleanup pass",
        "format": "langfuse_cleanup",
    },
    "bc-check": {
        "module": "runners.periodic_runner",
        "function": "run_bc_check_pass",
        "label": "BC-check pass",
        "format": "memory_drafts",
    },
    "completeness-check": {
        "module": "runners.periodic_runner",
        "function": "run_completeness_check_pass",
        "label": "Completeness-check pass",
        "format": "memory_drafts",
    },
    "run-health": {
        "module": "runners.run_health_runner",
        "function": "run_run_health_pass",
        "label": "Run-health pass",
        "format": "memory_notes",
    },
    "diagnostic": {
        "module": "runners.diagnostic_runner",
        "function": "run_diagnostic_pass",
        "label": "Diagnostic pass",
        "format": "drafts",
    },
    "survey": {
        "module": "runners.periodic_runner",
        "function": "run_survey_pass",
        "label": "Survey pass",
        "format": "memory_drafts",
    },
    "copy-paste": {
        "module": "runners.periodic_runner",
        "function": "run_copy_paste_pass",
        "label": "Copy-paste pass",
        "format": "memory_drafts",
    },
    "state-sync": {
        "module": "runners.periodic_runner",
        "function": "run_state_sync_pass",
        "label": "State-sync pass",
        "format": "memory_drafts",
    },
    "env-doc-sync": {
        "module": "runners.periodic_runner",
        "function": "run_env_doc_sync_pass",
        "label": "Env-doc-sync pass",
        "format": "memory_drafts",
    },
    "frontend-sync": {
        "module": "runners.periodic_runner",
        "function": "run_frontend_sync_pass",
        "label": "Frontend-sync pass",
        "format": "memory_drafts",
    },
    "forge-parity": {
        "module": "runners.periodic_runner",
        "function": "run_forge_parity_pass",
        "label": "Forge-parity pass",
        "format": "memory_drafts",
    },
    "module-curator": {
        "module": "runners.periodic_runner",
        "function": "run_module_curator_pass",
        "label": "Module-curator pass",
        "format": "memory_drafts",
    },
    "security-posture": {
        "module": "runners.periodic_runner",
        "function": "run_security_posture_pass",
        "label": "Security-posture pass",
        "format": "memory_drafts",
    },
    "roadmap-sync": {
        "module": "runners.roadmap_sync_runner",
        "function": "run_roadmap_sync_pass",
        "label": "Roadmap-sync pass",
        "format": "roadmap_sync",
    },
    "triage-boilerplate": {
        "module": "runners.periodic_runner",
        "function": "run_triage_boilerplate_pass",
        "label": "Triage-boilerplate pass",
        "format": "memory_drafts",
    },
    "verify": {
        "module": "runners.verify_runner",
        "function": "run_verify_pass",
        "label": "Verify",
        "format": "verify",
    },
    "meta": {
        "module": "meta.runner",
        "function": "run_meta_pass",
        "label": "Meta pass",
        "format": "memory_drafts",
    },
}
