Split diff-inspection helpers out of `git_ops.py` (1598 → ~1024 lines) into new `vcs/git_diff.py` module. All public names are re-exported through `git_ops` so no call site changes are needed.
