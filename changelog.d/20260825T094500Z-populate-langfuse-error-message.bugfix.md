Populate the Langfuse error message for failed stages: `_handle_stage_error` now records the exception on the active span, so trace summaries show the real error instead of `[ERROR] None`.
