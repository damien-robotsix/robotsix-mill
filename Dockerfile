# =============================================================================
# Stage 1: builder — temporary stage for build-time tooling and artifact
# production.  Nothing from this stage (except copied artifacts) lands in
# the final image.
# =============================================================================
FROM python:3.14-slim@sha256:7d8de339aa8619f9b25e7d474d687ae4f6ef9704adad90a136af0f485adeccb7 AS builder

# pipefail-aware shell so a failure on the LHS of a pipe (e.g. the
# sha256 verification `echo … | sha256sum -c -`) propagates and the
# whole RUN aborts. Also satisfies hadolint DL4006.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Acquire::Retries lets apt recover from transient mirror glitches.
# Apt version pin validated against Debian Trixie (13) — see same note in
# the base stage.  curl is only needed in the builder for the Docker CLI
# download; it is NOT carried into the production image.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
        curl=8.14.1-* \
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
ARG DOCKER_CLI_SHA256_amd64
ARG DOCKER_CLI_SHA256_arm64
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
# Copy only what pip needs to install the package (avoids baking the full
# source tree into the production image).
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --root-user-action=ignore ".[${INSTALL_EXTRAS}]"

# =============================================================================
# Stage 2: base — shared runtime setup (not built directly; extended by
# production and dev).
# =============================================================================
FROM python:3.14-slim@sha256:7d8de339aa8619f9b25e7d474d687ae4f6ef9704adad90a136af0f485adeccb7 AS base

# Acquire::Retries lets apt recover from transient mirror glitches.
# Apt version pins validated against Debian Trixie (13) as shipped in
# python:3.14-slim (check /etc/os-release in the base image).  Wildcard
# suffixes allow patch-level updates without changing the Dockerfile;
# bump these pins when the base image moves to a new Debian release.
# nodejs and npm are left unpinned — the Trixie versions change
# frequently; jscpd only needs a reasonably recent Node.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
        git=1:2.47.3-* \
        ca-certificates=20250419 \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Copy only the artifacts built in the builder stage — no source tree.
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin/docker /usr/local/bin/docker
COPY --from=builder /usr/local/bin/robotsix-mill /usr/local/bin/robotsix-mill

# Non-root user.  UID 1000 matches the typical first-host-user UID so the
# named volume lines up without extra chown when bind-mounted.
RUN groupadd --system --gid 1000 mill \
    && useradd --system --gid mill --uid 1000 --create-home --shell /bin/bash mill \
    && mkdir -p /data \
    && chown -R mill:mill /data

WORKDIR /app

ENV MILL_DATA_DIR=/data
# Bind the API on all interfaces inside the container so a future web
# frontend / published port can reach it; still localhost-only unless
# the port is published in compose.
ENV MILL_API_HOST=0.0.0.0
ENV MILL_API_URL=http://127.0.0.1:8077
EXPOSE 8077

# Health check uses Python stdlib (no curl needed).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://localhost:8077/health')" || exit 1

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

# Layer dev tooling (pytest, mypy, ruff, bandit) on top of the
# site-packages inherited from base.
ARG INSTALL_EXTRAS=dev,tracing
RUN pip install --no-cache-dir --root-user-action=ignore -e ".[${INSTALL_EXTRAS}]" \
    && chown -R mill:mill /app

USER mill

# =============================================================================
# Stage 4: production — minimal runtime image (DEFAULT target when no
# --target is given, because it is the last stage in the file).
# =============================================================================
FROM base AS production

# Production image carries only entrypoint.sh — no src/, tests/, or
# pyproject.toml.
COPY entrypoint.sh /app/entrypoint.sh

USER mill
