# CI gate-or-remove policy

This is the governing policy for every CI check in this repository. It
states the principle behind the recent "gate-or-remove" cleanup and
synthesizes how each of the seven previously-advisory checks was
resolved. Per-check rationales live in their own config files and
workflows (linked below); this document is the umbrella principle and a
reviewer checklist for new CI additions.

## The gate-or-remove principle

> No CI step may detect errors or findings that are *neither* blocking
> *nor* explicitly documented as an accepted advisory policy.

Every CI check must land in exactly one of three states:

1. **Gate** — the step fails the build on findings. This is the default
   for any check whose findings are actionable and worth blocking on.
2. **Explicitly-accepted advisory** — the step surfaces findings (CI
   annotations, logs, the Security tab) but does **not** block. This is
   only acceptable when the non-gating decision is documented with a
   written rationale in **two** places: a comment in the relevant
   config file or workflow, **and** a note in the
   [`CONTRIBUTING.md`](../CONTRIBUTING.md) "CI overview" section. An
   advisory check should also document its **promotion path** — the
   condition under which it becomes a hard gate.
3. **Removed** — a check that neither gates nor is explicitly accepted
   as advisory must be deleted. A step that produces findings nobody
   blocks on and nobody has signed off as advisory is "advisory limbo":
   noise that erodes the signal of the rest of CI.

The failure mode this policy exists to prevent is advisory limbo
creeping back in: a well-intentioned new check that surfaces findings,
gates on nothing, and carries no written acceptance — so contributors
learn to ignore it and its signal is lost.

## Synthesis of the seven resolved checks

| Check | Resolution | Where documented |
|-------|------------|------------------|
| **jscpd** (duplicate code) | **Removed** — no longer in any workflow; [`.jscpd.json`](../.jscpd.json) is preserved only as config for the on-demand `detect_duplication` agent tool. | `.jscpd.json` (config retained for the tool, not CI) |
| **mypy `--strict`** | **Advisory** — runs on every PR/push, emits an error-count CI annotation, `exit 0` so it never blocks (pre-existing ~700-error strict-mode backlog). Promotion: remove the advisory wrapper once the backlog is cleared. | Comment in [`ci.yml`](../.github/workflows/ci.yml) (Mypy step) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) "CI overview" |
| **Bandit** | **Gate (with severity floor)** — `--severity-level medium` blocks on MEDIUM+ findings; LOW findings are an intentionally non-blocking backlog. | `[tool.bandit]` comment in [`pyproject.toml`](../pyproject.toml) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| **Trivy gate** (`docker-publish.yml`) | **Gate (narrowed)** — only CRITICAL, fixable (`ignore-unfixed: true`) CVEs fail the pipeline; HIGH/MEDIUM/LOW are high-volume/low-signal and do not gate. | Comments in [`docker-publish.yml`](../.github/workflows/docker-publish.yml) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) "Trivy vulnerability scanning"; [`.trivyignore`](../.trivyignore) per-CVE escape hatch |
| **Trivy SARIF upload** | **Advisory (observability-only)** — a separate Trivy run emits SARIF for all severities with no `exit-code` and uploads to the GitHub Security tab; never blocks. | Comments in [`docker-publish.yml`](../.github/workflows/docker-publish.yml) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) "Trivy vulnerability scanning" |
| **hadolint** | **Advisory** — runs with `failure-threshold: warning`; warnings surface as CI annotations but do not block. Promotion: raise threshold to `error` once the Dockerfiles clear their known warnings. | [`.hadolint.yaml`](../.hadolint.yaml) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) "CI overview" |
| **pip-audit** | **Gate (with justified suppressions)** — fails on known vulnerabilities; two CVEs are suppressed via `--ignore-vuln`, each justified inline: **PYSEC-2025-183** (pyjwt — key strength is the caller's responsibility, no fix published) and **MAL-2026-4750** (fastapi — the malicious dep ships only in `fastapi[standard]`, which mill never installs; advisory since withdrawn). | Inline comments in [`security-audit.yml`](../.github/workflows/security-audit.yml) |
| **pip-licenses** (license compliance) | **Gate (allowlist with per-package escape hatch)** — the `license-audit` job runs `pip-licenses --allow-only=…` over the installed `.[tracing]` tree and fails on **any** dependency whose license is not on the permissive allowlist (MIT, Apache-2.0, BSD, ISC, PSF, MPL-2.0, Unlicense), so copyleft (GPL/AGPL/LGPL) **and** unlicensed/`UNKNOWN` deps both fail; `--fail-on="GPL;AGPL;LGPL"` is kept as explicit belt-and-suspenders. Escape hatch: `--ignore-packages` suppresses the first-party MIT robotsix git deps that can report `UNKNOWN` metadata, plus rare third-party deps that are verifiably permissive but whose metadata format defeats the allowlist (e.g. `tiktoken`, which reports the full MIT license text instead of a short token), each justified inline. Policy lives in the workflow's CLI flags + comments (no separate `.licenserc`/`.scancode` file), matching the pip-audit `--ignore-vuln` / `[tool.bandit]` inline-policy convention. | Inline comments in [`security-audit.yml`](../.github/workflows/security-audit.yml) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) "CI overview" |
| **commitizen** (commit-msg pre-commit hook) | **Gate (local only, not CI)** — the `commitizen` hook in `.pre-commit-config.yaml` validates every commit message against the Conventional Commits spec before it is accepted. Developers must run `.venv/bin/pre-commit install --hook-type commit-msg` once. Not a CI check; enforced at authoring time. Promotion path: add a CI step to validate commit messages on PRs if convention drift is observed. | Comment in [`.pre-commit-config.yaml`](../.pre-commit-config.yaml) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) "Commit messages" |
| **python-semantic-release** (release.yml) | **Automation (not a gate)** — the `release.yml` workflow runs `uv run semantic-release publish` on every push to `main`. It parses conventional-commit history, computes the next version, updates `pyproject.toml`, auto-generates `CHANGELOG.md`, creates a Git tag + GitHub Release, and triggers PyPI publishing. Not a CI gate — it is a delivery pipeline that exits silently when no new version is needed. | Comments in [`.github/workflows/release.yml`](../.github/workflows/release.yml) + [`CONTRIBUTING.md`](../CONTRIBUTING.md) "CI overview" + [`docs/publishing.md`](publishing.md) |

## Reviewer checklist for a new CI check

When reviewing a PR that adds or changes a CI step, walk this checklist.
A new check should not merge until each box is either checked or
consciously waived with a note.

- [ ] **Does it gate, or is it explicitly accepted as advisory?** If it
      does neither (surfaces findings but blocks on nothing, with no
      written acceptance), it is advisory limbo — gate it or remove it.
- [ ] **If advisory, is the rationale written down in both places?** A
      comment in the config file or workflow **and** a note in the
      [`CONTRIBUTING.md`](../CONTRIBUTING.md) "CI overview" section.
- [ ] **If advisory, is there a promotion path?** The condition under
      which the check becomes a hard gate (e.g. "once the backlog is
      cleared", "once the Dockerfiles mature").
- [ ] **If gating, is there an escape hatch?** A documented mechanism
      for accepting known false positives (e.g. `.trivyignore`,
      `--ignore-vuln`, a `skips` list) with a per-entry justification
      and, where relevant, an expiry.
- [ ] **Is it discoverable from [`CONTRIBUTING.md`](../CONTRIBUTING.md)?**
      The "CI overview" table and notes should reflect the new check so
      a contributor can find it without reading workflow YAML.

## Committed-lockfile gate (`ci.yml`)

`ci.yml`'s `uv sync --frozen` step is a hard **gate** (not advisory):
it installs from the committed `uv.lock` and fails the build if the lock
is stale relative to `pyproject.toml`. The escape hatch is the automated
`uv.lock` bump PR ([`deps-bump.yml`](../.github/workflows/deps-bump.yml));
full rationale lives in [`dependencies.md`](dependencies.md).

## See also

- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — "CI overview" section with
  per-check notes and the workflow table.
- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml),
  [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml),
  [`.github/workflows/security-audit.yml`](../.github/workflows/security-audit.yml)
  — the workflows these decisions live in.
