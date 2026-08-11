# Changelog fragments

Each non-trivial PR must add a fragment file in this directory so the
release workflow can assemble a curated `CHANGELOG.md`.

## Fragment naming

`<ticket-slug>.<type>.md`

- `<ticket-slug>` — a short, filesystem-safe identifier for the change
  (e.g. the ticket slug or issue number).  Use only alphanumerics,
  hyphens, and underscores.
- `<type>` — one of: `feature`, `bugfix`, `doc`, `removal`, `misc`.

Example: `20260809T154813Z-adopt-towncrier.misc.md`

## Fragment content

A single Markdown bullet line (no leading `- ` — towncrier adds it):

```markdown
Adopt towncrier to assemble the mill's own changelog.
```

Multi-paragraph entries are fine; continuation lines are indented with
two spaces:

```markdown
Adopt towncrier to assemble the mill's own changelog.
  Fragments live under `changelog.d/` and are assembled by
  `towncrier build` at release time.
```

## Assembly

At release time, run `towncrier build` to consume all fragments and
update `CHANGELOG.md`.  The fragments are automatically deleted after
a successful build.
