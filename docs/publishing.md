# Publishing to PyPI

Releases of the `robotsix-mill` distribution are published automatically.

## How releases publish

Publishing is automated. Creating and **publishing a GitHub Release**
triggers [`.github/workflows/release.yml`](https://github.com/robotsix/mill/blob/main/.github/workflows/release.yml),
which calls the reusable `python-release.yml` workflow to:

1. Build the sdist + wheel (`uv build`).
2. Attach the built distributions to the triggering GitHub Release.
3. Upload them to PyPI as the `robotsix-mill` distribution via OIDC
   **trusted publishing** (no API token).

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

## Version-bump requirement

`pyproject.toml` carries a hardcoded `[project].version` (currently
`0.0.1`); there is no dynamic/VCS version source. PyPI **rejects
re-uploading an existing version**, so before each release maintainers
MUST bump `[project].version` in `pyproject.toml` and commit it. The
release tag/version should match the `pyproject.toml` version.

## Maintainer step-by-step flow

1. Bump `[project].version` in `pyproject.toml`.
2. Commit and merge the bump to `main`.
3. Create a GitHub Release (with a tag matching the new version) and
   publish it.
4. Automation builds the distributions, attaches them to the release,
   and publishes to PyPI.

## Optional hardening

For extra protection you can (optionally — not currently implemented)
add a dedicated GitHub `environment` (e.g. `pypi`) with required
reviewers to the `publish` job and require that environment in the PyPI
trusted-publisher configuration. This gates each publish behind a manual
approval.
