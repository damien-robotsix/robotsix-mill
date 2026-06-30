# vulture_whitelist.py — framework-invoked names that vulture would otherwise
# flag as unused.  Keep this file: vulture scans it alongside the source tree
# and considers names referenced here as "used".
#
# This file is NOT imported — bare-name expressions at module scope are
# evaluated by Python (no-op) and seen as usage by vulture's AST scan.
# If a name no longer exists in the source tree, Python raises a NameError
# at scan time, catching stale entries.
#
# Ruff B018 ("useless expression") is suppressed on this file via
# [tool.ruff.lint.per-file-ignores] because the bare-name pattern is the
# intentional API of vulture whitelists.

# ---------------------------------------------------------------------------
# CLI entry points (called via console_scripts in pyproject.toml)
# ---------------------------------------------------------------------------
main

# ===========================================================================
# Pre-existing dead code — grandfathered so vulture only catches *new* dead
# code.  Each name below is genuinely unused today but kept because it is
# either part of a not-yet-wired feature surface, a periodic-pass skeleton,
# a pydantic validator hook, or a stub that exists for backward compatibility.
# ===========================================================================

# -- agents ------------------------------------------------------------------
code_fix_possible
code_fix_description
run_agent_check_agent
run_audit_agent
run_bc_check_agent
reset_for_tests
reset_board_manager_for_tests
run_completeness_check_agent
run_config_sync_agent
run_env_doc_sync_agent
run_security_posture_agent
_absorb_summary_typos
best_k
failure_summary
iterations_used
run_copy_paste_agent
run_frontend_sync_agent
# Result-type aliases (PeriodicAgentResult) — consumed by tests
AuditResult
BcCheckResult
CompletenessCheckResult
CopyPasteResult
ForgeParityResult
FrontendSyncResult
HealthResult
EnvDocSyncResult
StateSyncResult
SurveyResult
TestGapResult
SecurityPostureResult
model_config
chunk_size
max_chunks
extras
create_expert
match_module_paths
get_expert
remove_expert
close_all
run_forge_parity_agent
run_health_agent
run_module_curator_agent
_absorb_spec_markdown_typos
output_context
run_survey_agent
run_test_gap_agent
run_triage_boilerplate_agent
run_state_sync_agent
parameters
web_fetch_budget
correct_form

# -- cli ---------------------------------------------------------------------
returncode_on_failure

# -- config ------------------------------------------------------------------
transient_retries
transient_backoff_base
transient_backoff_cap
rate_limit_backoff_base
rate_limit_backoff_cap
reference_files_max_count
reference_files_max_total_lines
gitlab_api_url
audit_periodic
audit_interval_seconds
trace_health_periodic
trace_review_periodic
trace_review_interval_seconds
timeout_escalation_periodic
test_gap_periodic
test_gap_interval_seconds
agent_check_periodic
agent_check_interval_seconds
health_periodic
health_interval_seconds
survey_periodic
survey_interval_seconds
bc_check_periodic
bc_check_interval_seconds
module_curator_memory_path
module_curator_periodic
module_curator_interval_seconds
data_dir_gc_periodic
data_dir_gc_interval_seconds
completeness_check_periodic
completeness_check_interval_seconds
forge_parity_memory_path
forge_parity_periodic
forge_parity_interval_seconds
state_sync_periodic
state_sync_interval_seconds
copy_paste_memory_path
copy_paste_periodic
state_sync_model
copy_paste_interval_seconds
triage_boilerplate_memory_path
triage_boilerplate_periodic
triage_boilerplate_interval_seconds
config_sync_periodic
config_sync_interval_seconds
env_doc_sync_model
env_doc_sync_memory_path
env_doc_sync_periodic
env_doc_sync_interval_seconds
member_sync_interval_seconds
meta_periodic
security_posture_memory_path
security_posture_periodic
security_posture_interval_seconds
run_health_periodic
run_health_memory_path
diagnostic_periodic
stale_branch_cleanup_periodic
db_maintenance_periodic
sandbox_reaper_periodic
dependabot_ingest_periodic
orphaned_pr_check_periodic
# board_manager_max_concurrent — dedicated board-manager Claude semaphore lane
# (wired via handle_wrapper in lifespan.py once upstream robotsix-board-agent
#  supports the kwarg; guarded by an inspect.signature check.)
board_manager_max_concurrent
scope_triage_request_limit
already_done_request_limit
langfuse_cleanup_periodic
settings_customise_sources
dotenv_settings
retrospect_memory_file
trace_inspector_memory_file
audit_memory_file
agent_check_memory_file
health_memory_file
test_gap_memory_file
survey_memory_file
config_sync_memory_file
env_doc_sync_memory_file
state_sync_memory_file
bc_check_memory_file
completeness_check_memory_file
implement_memory_file
refine_memory_file
doc_memory_file
ci_fix_memory_file
rebase_memory_file
ci_patterns_file
get_field_value

# -- core --------------------------------------------------------------------
_set_wal  # SQLAlchemy event listener registered via @event.listens_for decorator
DATA_DIR_GC
impl
cache_ok
process_bind_param
dialect
process_result_value
reset_engine
default_service
format_duration
MEMBER_SYNC
workspace_path
origin_session_url
unmet_deps
CommentRead

# -- forge -------------------------------------------------------------------
list_pr_reviews
list_review_comments
close_pr
post_pr_comment

# -- langfuse ----------------------------------------------------------------

# -- runtime routes (FastAPI decorator-registered handlers) ------------------
get_trace_detail

# -- meta --------------------------------------------------------------------
todo_drafts_created
MARKERS

# -- repo_scaffold -----------------------------------------------------------

# -- deploy-server ------------------------------------------------------------
# Pydantic model fields (DeploySettings) — accessed via string-based
# env-var binding; vulture does not trace pydantic-settings Field() usage.
broker_url
langfuse_host
# FastAPI route handler — invoked via @app.get("/ready") decorator, not
# by direct Python call. Tested via HTTP TestClient.
ready

# -- runners -----------------------------------------------------------------
run_agent_check_pass
run_audit_pass
run_bc_check_pass
run_completeness_check_pass
run_config_sync_pass
run_env_doc_sync_pass
run_copy_paste_pass
run_security_posture_pass
run_data_dir_gc_pass
dir_size_bytes
# Consumed by tests through imports and isinstance checks.
AuditPassResult
AgentCheckPassResult
BcCheckPassResult
SurveyPassResult
CompletenessCheckPassResult
CopyPastePassResult
ForgeParityPassResult
ConfigSyncPassResult
HealthPassResult
ModuleCuratorPassResult
TestGapPassResult
oversized_items
query_traces_since
query_recent_traces
query_session_summary
human_pr_skipped
run_forge_parity_pass
run_frontend_sync_pass
run_health_pass
run_module_curator_pass
__test__
__qualname__
raw_span
run_roadmap_sync_pass
run_state_sync_pass
run_survey_pass
run_test_gap_pass
run_triage_boilerplate_pass
traces_scanned
traces_flagged
run_trace_review_pass
run_verify_pass

# -- vcs ---------------------------------------------------------------------
# Tested git utility with no current production caller: ci_fix's proactive
# rebase (its only caller) was removed so branch-own CI failures go straight
# to the fix agent (c14c). Kept as a reusable, unit-tested helper.
branch_is_behind_main

# -- runtime -----------------------------------------------------------------
Instrumentator
BoardAdapter
instance
move_endpoint
move_endpoint_template
render_mode
get_broadcaster
list_enabled_agents
board_cards
board_move
list_candidates
validate_candidate
reject_candidate
cost_breakdown
create_epic
generate_children
credit_status
credit_status_clear
health
health_live
health_ready
langfuse_status
langfuse_status_clear
worker_status
list_repos
gates
ws_board
trace_health_check
langfuse_cleanup_pass
meta_pass
create_ticket
list_tickets
get_ticket
get_history
get_description
upload_screenshot
get_retrospect
list_artifacts
get_artifact
delete_ticket
convert_to_task
migrate_ticket
approve_ticket
merge_now
get_merge_info
get_merge_reason
get_merge_status
list_runs
list_active
finished_at
_audit_task
_trace_health_task
_trace_review_task
_health_task
_agent_check_task
_bc_check_task
_completeness_check_task
_copy_paste_task
_module_curator_task
_test_gap_task
_survey_task
_config_sync_task
_data_dir_gc_task
_langfuse_cleanup_task
_timeout_escalation_task
_meta_task
_run_health_task
_diagnostic_task
_stale_branch_task
_orphaned_pr_check_task
_db_maintenance_task
_sandbox_reaper_task
_dependabot_ingest_task
_requeue_task
queue_size
queue_join
_run_periodic_pass_per_repo

# -- stages ------------------------------------------------------------------
input_state
# RefineAgentMixin delegation methods — called from tests (test_refine_orchestration.py)
# and via RefineStage class inheritance; vulture (60% confidence) doesn't trace test calls.
_review_spec_conciseness
_short_circuit_for_internal_failure
# build_resume_message_history — kept for debugging/rollback; replaced by
# build_compact_resume_message_history in the pause/resume handoff.
build_resume_message_history
