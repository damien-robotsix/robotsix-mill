`Settings()` reads `config/config.json` again. The clean-cutover to the
robotsix config-standard (#2525) removed the model's JSON source and replaced
it with a `load_settings()` helper that was never wired to a caller — and
nothing else reads the file, so every one of the several hundred bare
`Settings()` constructions across the codebase silently fell back to model
defaults. The commit had not been deployed yet; the next deploy would have
reverted mill's entire runtime configuration, `MILL_MAX_GLOBAL_CONCURRENCY`
included. The file source is restored below `os.environ`, and
`tests/config/test_config.py` now pins the precedence.
