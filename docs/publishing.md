# Publishing to PyPI

Releases of the `robotsix-mill` distribution are published automatically.

## How releases publish

Publishing is fully automated via
[`python-semantic-release`](https://python-semantic-release.readthedocs.io/).
Every push to `main` triggers
[`.github/workflows/release.yml`](../.github/workflows/release.yml), which runs
`uv run semantic-release publish` to:

1. Parse conventional-commit history since the last tag.
2. Compute the next version (major/minor/patch) from the commit types.
3. Update `pyproject.toml`'s `[project].version` and the `__version__` variables
   in `src/robotsix_mill/__init__.py`.
4. Auto-generate `CHANGELOG.md` from the commit messages.
5. Create a Git tag and a GitHub Release with auto-generated release notes.
6. Build the sdist + wheel (`uv build`).
7. Upload the built distributions to PyPI as the `robotsix-mill` distribution
   via OIDC **trusted publishing** (no API token).

Commit messages must follow the [Conventional Commits](https://www.conventionalcommits.org/)
format (enforced by the `commitizen` commit-msg pre-commit hook — see
[`CONTRIBUTING.md`](../CONTRIBUTING.md)). The type determines the version bump:

- `fix:` commits trigger a **patch** bump (0.0.x).
- `feat:` commits trigger a **minor** bump (0.x.0).
- `feat!:` or `BREAKING CHANGE:` footers trigger a **major** bump (x.0.0).

## Required one-time PyPI setup

The publish step depends on a one-time configuration that **must be done
by a human on the PyPI website** — the agent/CI cannot perform it:

- Configure **Trusted Publishing** for the `robotsix-mill` project on
  PyPI, registering this repository and the **caller workflow filename**
  as a trusted publisher.
- PyPI matches the **top-level workflow filename that triggers the run**,
  i.e. `release.yml` — **not** the reusable `python-release.yml`. Use
  `release.yml` when registering the trusted publisher.

Until this is configured on PyPI, the `publish` job will fail.

## Versioning

Versioning is fully automated — **do not manually bump** `pyproject.toml`
or the `__version__` variables. `python-semantic-release` reads the commit
history since the last tag and derives the correct next version. The
`pyproject.toml` version is a hardcoded starting point (currently `0.0.1`);
semantic-release updates it on each publish.

## Maintainer step-by-step flow

1. Ensure commits on `main` follow [Conventional Commits](https://www.conventionalcommits.org/)
   format (enforced by the `commitizen` pre-commit hook locally).
2. Push to `main`.
3. Automation handles version bump, CHANGELOG generation, GitHub Release
   creation, and PyPI upload — no manual intervention needed.
4. If no release is published (e.g. no new conventional commits since the
   last tag), semantic-release simply exits without changes.

## Optional hardening

For extra protection you can (optionally — not currently implemented)
add a dedicated GitHub `environment` (e.g. `pypi`) with required
reviewers to the `publish` job and require that environment in the PyPI
trusted-publisher configuration. This gates each publish behind a manual
approval.
