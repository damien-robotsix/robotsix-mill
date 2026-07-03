# =============================================================================
# Stage 1: builder — temporary stage for build-time tooling and artifact
# production.  Nothing from this stage (except copied artifacts) lands in
# the final image.
# =============================================================================
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS builder

# pipefail-aware shell so a failure on the LHS of a pipe (e.g. the
# sha256 verification `echo … | sha256sum -c -`) propagates and the
# whole RUN aborts. Also satisfies hadolint DL4006.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Acquire::Retries lets apt recover from transient mirror glitches.
# Apt version pin validated against Debian Trixie (13) — see same note in
# the base stage.  curl is only needed in the builder for the Docker CLI
# download; git is needed so uv can clone git dependencies (robotsix-llmio).
# Neither is carried into the production image.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
        curl=8.14.1-* \
        git=1:2.47.3-* \
    && rm -rf /var/lib/apt/lists/*

# ── Docker CLI static binary ─────────────────────────────────────────────
# Pin a specific release.  SHA256 verification runs when the build-arg is
# supplied; otherwise the build prints a warning and continues (CI-safe
# default).  For a locked-down production build, set both:
#   docker build --build-arg DOCKER_CLI_SHA256_amd64=<sha> ...
#
# NOTE: Docker does NOT publish checksums for the docker binary.
# Compute the SHA256 of the extracted binary yourself:
#   curl -sL https://download.docker.com/linux/static/stable/x86_64/docker-29.5.1.tgz | tar -xzO docker/docker | sha256sum
#   curl -sL https://download.docker.com/linux/static/stable/aarch64/docker-29.5.1.tgz | tar -xzO docker/docker | sha256sum
ARG DOCKER_CLI_VERSION=29.5.1
ARG DOCKER_CLI_SHA256_amd64=ae01aca0e05d07e39bc5e8fbbee698ce365c417e36c90b3c9803b3af5f344742
ARG DOCKER_CLI_SHA256_arm64=fac73e803fdbebd28b75eda6963f5a6ea0b3944039396befd443a0b23cb28091
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) DARCH=x86_64;  EXPECTED="$DOCKER_CLI_SHA256_amd64" ;; \
         arm64) DARCH=aarch64; EXPECTED="$DOCKER_CLI_SHA256_arm64" ;; \
         *)     DARCH="$ARCH"; EXPECTED="" ;; \
       esac \
    && URL="https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
    && curl -fsSL "$URL" -o /tmp/docker.tgz \
    && tar -xz -C /usr/local/bin --strip-components=1 -f /tmp/docker.tgz docker/docker \
    && if [ -n "$EXPECTED" ]; then \
           echo "${EXPECTED}  /usr/local/bin/docker" | sha256sum -c -; \
       else \
           echo "WARNING: Docker CLI checksum not verified — supply DOCKER_CLI_SHA256_${ARCH} build-arg to verify"; \
       fi \
    && docker --version \
    && rm /tmp/docker.tgz

# ── Python project installation ──────────────────────────────────────────
# INSTALL_EXTRAS controls which optional-dependency set lands in
# site-packages.  Default is "tracing" (OpenTelemetry/Langfuse) —
# no dev toolchain.  The dev stage re-installs with "dev,tracing".
ARG INSTALL_EXTRAS=tracing
WORKDIR /build
# Copy only what uv needs to install the package (avoids baking the full
# source tree into the production image).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY agent_definitions/ ./agent_definitions/
COPY expert_definitions/ ./expert_definitions/
COPY skills/ ./skills/
# contrib/completions is force-included into the wheel (pyproject.toml
# [tool.hatch.build.targets.wheel.force-include]); it must be in the build
# context or the wheel build fails with "Forced include not found".
COPY contrib/ ./contrib/
# DO NOT switch this to `uv sync` / `UV_PROJECT_ENVIRONMENT=system`. That
# env var is a venv PATH, not a mode: uv builds a venv at /build/system and
# puts the `robotsix-mill` console script at /build/system/bin, so the base
# stage's `COPY --from=builder /usr/local/bin/robotsix-mill` fails with
# "not found" and the image won't build. `uv pip install --system` targets
# the system interpreter (/usr/local), keeps uv's speed, and lands the
# script where the COPY expects it. (Regressed twice — see PR #491.)
# hadolint ignore=SC2086
RUN pip install uv --no-cache-dir \
    && EXTRA_FLAGS="" \
    && IFS=',' read -ra _extras <<< "${INSTALL_EXTRAS}" \
    && for e in "${_extras[@]}"; do [ -n "$e" ] && EXTRA_FLAGS="$EXTRA_FLAGS --extra $e"; done \
    && uv export --frozen --no-emit-project --no-default-groups $EXTRA_FLAGS \
         --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm -f /tmp/requirements.txt

# =============================================================================
# Stage 2: base — shared runtime setup (not built directly; extended by
# production and dev).
# =============================================================================
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS base

# Acquire::Retries lets apt recover from transient mirror glitches.
# Apt version pins validated against Debian Trixie (13) as shipped in
# python:3.14-slim (check /etc/os-release in the base image).  Wildcard
# suffixes allow patch-level updates without changing the Dockerfile;
# bump these pins when the base image moves to a new Debian release.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
        git=1:2.47.3-* \
        ca-certificates=20250419* \
        nodejs=20.19.2+dfsg-1+deb13u2 \
        npm=9.2.0~ds1-3 \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (`gh`) for driving contribution workflows (push -> PR -> merge)
# from inside the sandbox. `gh` lives in the app image too because the
# dev-pinned sandbox runs `robotsix/mill:dev` (config/config.yaml
# `sandbox.image`), not `robotsix/mill-sandbox`, so it must be in the `base`
# stage to reach that path; every image derived from base (dev, production)
# inherits it. `gh` is not in Debian's default apt repos, so add the official
# GitHub CLI apt source first. `curl` is not present in `base` (unlike the
# sandbox image), so install it in this same layer to fetch the keyring;
# `docker build` has full host network (the runtime egress proxy does not
# apply at build time), so fetching the keyring here is fine.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && rm -rf /var/lib/apt/lists/*

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Claude Agent SDK transport (opt-in via llm_backend / claude_sdk_agents):
# the `claude-agent-sdk` Python dep ships its OWN self-contained `claude`
# binary (claude_agent_sdk/_bundled/claude) and drives THAT — so no global
# `npm install -g @anthropic-ai/claude-code` is needed (it was dead weight,
# and a version skew between it and the bundled binary only caused confusion).
# nodejs/npm above stay for `npx jscpd@4` (copy-paste detection), not Claude.
# Subscription auth comes from a mounted ~/.claude dir (see
# docker-compose.override.example.yml); the worker runs as the non-root `mill`
# user so the SDK's `--dangerously-skip-permissions` is accepted.

# Copy only the artifacts built in the builder stage — no source tree.
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin/docker /usr/local/bin/docker
COPY --from=builder /usr/local/bin/robotsix-mill /usr/local/bin/robotsix-mill
# uv must be ON $PATH and world-executable here in `base` for the SAME reason
# as `gh` above: the live sandbox runs `robotsix/mill:dev` (config
# `sandbox.image`), so `uv lock`/`uv sync` are unusable unless uv is in the
# base stage that dev extends. A fresh `pip install uv` in a child stage is a
# no-op (uv is already in the inherited site-packages but its /usr/local/bin
# launcher was not copied → "uv: not found"), so copy the launcher explicitly.
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

# Non-root user.  UID 1000 matches the typical first-host-user UID so the
# named volume lines up without extra chown when bind-mounted.
RUN groupadd --system --gid 1000 mill \
    && useradd --system --gid mill --uid 1000 --create-home --shell /bin/bash mill \
    && mkdir -p /data \
    && chown -R mill:mill /data \
    && mkdir -p /app/.data \
    && chown -R mill:mill /app/.data \
    # Pre-create a mill-owned ~/.claude so the Claude SDK transport's bundled
    # `claude` CLI can write its state/cache there; the host's whole ~/.claude
    # dir is bind-mounted rw (see docker-compose.override.example.yml).
    && mkdir -p /home/mill/.claude \
    && chown -R mill:mill /home/mill/.claude

WORKDIR /app

# Per-deploy cache-busting token for static asset URLs (asset_version()).
# Optional: absent build-arg → empty ENV → runtime process-start fallback.
ARG MILL_BUILD_SHA=
ENV MILL_BUILD_SHA=${MILL_BUILD_SHA}

# Runtime config used to be set here via MILL_* env vars (data_dir,
# api_host, api_url). The MILL_* alias surface was retired in
# 9cd2630; the equivalent settings now live in config/config.yaml
# (data_dir, api_host, api_url) and ship to the container via the
# config/ bind-mount in docker-compose.yml. See config/config.example.yaml
# for the canonical operator surface.
EXPOSE 8077

# Health check uses Python stdlib (no curl needed).
# timeout is generous (25s, not 5s): under legitimate heavy load the single
# event loop can briefly delay even /health/live, and a 5s timeout flipped the
# container to `unhealthy` during normal busy periods. retries=3 still catches
# a genuine hang.
HEALTHCHECK --interval=30s --timeout=25s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://localhost:8077/health/live')" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

# =============================================================================
# Stage 3: dev — extends base for local sandbox use.
#
# Build with:
#   docker build --target dev -t robotsix/mill:dev .
#
# This stage is selected by docker-compose.override.yml (build.target: dev).
# =============================================================================
FROM base AS dev

USER root

# Copy the full source tree for sandbox test runs.
COPY . /app

# Layer dev tooling (pytest, mypy, ruff, bandit, robotsix-modules) on top of
# the site-packages inherited from base.
#
# Use uv (not pip): the `dev` dependency-group includes git-only deps
# (robotsix-modules, robotsix-agent-comm) declared in [tool.uv.sources].
# pip does NOT read [tool.uv.sources], so `pip install --group dev` resolves
# the bare name `robotsix-modules` to an unrelated PyPI squatter (pinned to
# Python <3.11) and fails with "no matching distribution" — which silently
# broke every :dev image build from 2026-06-26. uv reads [tool.uv.sources]
# and resolves them from the frozen lockfile, exactly like the builder stage.
# The base stage carries the builder's site-packages but not the uv launcher,
# so copy the binary across before using it.
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
# hadolint ignore=SC2086
RUN uv export --frozen --no-emit-project --extra tracing --group dev \
        --format requirements-txt -o /tmp/dev-requirements.txt \
    && uv pip install --system --no-cache -r /tmp/dev-requirements.txt \
    && uv pip install --system --no-cache --no-deps -e . \
    && rm -f /tmp/dev-requirements.txt \
    && chown -R mill:mill /app

# Entrypoint runs as root, joins the host's docker.sock group, then
# drops to mill via runuser. (No USER mill here — the privilege drop is
# in entrypoint.sh.)

# =============================================================================
# Stage 4: production — minimal runtime image (DEFAULT target when no
# --target is given, because it is the last stage in the file).
# =============================================================================
FROM base AS production

# Production image carries only entrypoint.sh — no src/, tests/, or
# pyproject.toml.
COPY entrypoint.sh /app/entrypoint.sh

# central-deploy support: the mill now reads a SINGLE config file,
# /app/config/config.yaml, which central-deploy writes into the
# mill-config named volume (every non-secret knob plus a top-level
# `secrets:` block). The deploy entrypoint just chmod 600's it and hands
# it to the runtime user — no defaults-seeding or split step. See
# deploy/docker-compose.yml and config/config.example.yaml.

# Entrypoint runs as root, joins the host's docker.sock group, then
# drops to mill via runuser. (No USER mill here.)
