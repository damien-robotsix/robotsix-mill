The implement agent now records changelog entries as towncrier fragments
(`add_changelog_fragment`) instead of inserting bullets into `CHANGELOG.md`
(`insert_changelog_entry`). Every ticket previously wrote to the same spot under
`## 0.0.0 (unreleased)`, so any two open PRs conflicted pairwise — a
combinatorial problem no `gh pr update-branch` could resolve. Fragments are one
file per ticket, which is what the fleet standard requires and what makes
parallel PRs conflict-free. The fragment directory and valid types are read from
each repo's own `[tool.towncrier]`, since the fleet is not uniform.
