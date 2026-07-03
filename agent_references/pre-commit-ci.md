# Pre-commit CI failures: standard boilerplate fixes

The pre-commit CI pipeline fires on every ticket branch after
`implement_complete`. Four failure patterns account for nearly all
pre-commit CI failures. Apply the corresponding fix mechanically —
do not re-diagnose.

## 1. mdformat line wrapping (CHANGELOG.md >100 chars)

### Symptom

CI reports `mdformat` failure on `CHANGELOG.md`.

### Standard fix

Re-wrap the offending bullet entries at 100 characters. New entries go
on their own line with 2-space continuation indent:

```markdown
- Short entry on one line.
- Longer entry that spans more than 100 characters
  continues here with 2-space indent.
```

Run `mdformat CHANGELOG.md` to auto-fix, or manually reflow the
offending line.

## 2. Missing trailing newline (end-of-file-fixer)

### Symptom

CI reports `end-of-file-fixer` failure on a changelog fragment
(`.d/*.md`) or other text file.

### Standard fix

Add a single trailing newline at end of file:

```bash
echo '' >> path/to/file.md
```

Or open the file in an editor and ensure it ends with a blank line.

## 3. Stale detect-secrets baseline

### Symptom

CI reports `detect-secrets` failure with only a `generated_at`
timestamp change — no new secrets found.

### Standard fix

Regenerate the baseline:

```bash
detect-secrets scan --baseline .secrets.baseline
```

If no new secrets are found, only the timestamp line changes — this is
expected and safe. Commit the updated baseline.

## 4. Ruff formatting

### Symptom

CI reports `ruff format` check failure (or `ruff check` with
auto-fixable violations).

### Standard fix

Run ruff format and commit the result:

```bash
ruff format .
ruff check --fix .
```

Always run both commands — the shared CI workflow runs both
`ruff check` and `ruff format --check`, so a formatting-only fix
without a corresponding `ruff check --fix` can leave lint violations
and bounce CI back.
