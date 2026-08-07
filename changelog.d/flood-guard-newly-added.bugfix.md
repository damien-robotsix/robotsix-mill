The scope-triage flood guard now counts only files the branch newly introduced,
not every out-of-scope file. A build-artifact flood is thousands of new paths; a
cross-cutting refactor is edits to files that already existed. Counting both
alike blocked exactly the changes that are most tedious by hand — a
default-account removal touching 79 files and a mypy-gate promotion touching 71,
both correctly scoped and neither containing an artifact. The prompt-overflow
protection the cap also provided moves to a separate, far higher
`scope_triage_hard_max_files` ceiling.
