Restored the Langfuse test helpers' tracing setup. The config-standard cutover
moved the Langfuse credentials onto `Settings` itself, but the helpers in
`tests/langfuse/` still populated only the `Secrets` singleton — so
`Settings.tracing_enabled` was False, every runner short-circuited, and 17
tests asserted against empty results. They now set the `Settings` fields too.
