## Doc layout

- `CHANGELOG.md` — root-level changelog
- `docs/` — documentation root with many subdirs
- Source code in `src/robotsix_mill/forge/gitlab/core.py` has module-level docstrings
- `docs/vcs/README.md` — VCS clone/branch bootstrap and empty-repo handling docs

## Conventions

- Module-level docstrings at top of `.py` files
- CHANGELOG entries follow format: `- <description>.`
- No separate docs/ changelog — all in CHANGELOG.md
- Implement stage clone/branch logic in `src/robotsix_mill/stages/implement/file_operations.py` — `_clone_and_branch` handles bootstrap for empty remotes
