## Doc layout

- `CHANGELOG.md` — root-level changelog
- `docs/` — documentation root with many subdirs
- Source code in `src/robotsix_mill/forge/gitlab/core.py` has module-level docstrings
- `docs/vcs/README.md` — VCS clone/branch bootstrap and empty-repo handling docs
- `docs/forge/ci-monitoring.md` — CI monitoring docs covering `check_status` return shape (including the `jobs` field), log truncation, size caps, and edge cases

## Conventions

- Module-level docstrings at top of `.py` files
- CHANGELOG entries follow format: `- <description>.`
- No separate docs/ changelog — all in CHANGELOG.md
- Implement stage clone/branch logic in `src/robotsix_mill/stages/implement/file_operations.py` — `_clone_and_branch` handles bootstrap for empty remotes
- `check_status()` returns `jobs: [{"name": str, "conclusion": str | None}]` giving a flat per-job list for all CI jobs/checks, so callers don't need to hit an external API for per-job status
