FROM python:3.14-slim

# git: the implement stage branches/commits on per-ticket work trees.
# Acquire::Retries lets apt recover from transient mirror glitches.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI — *client binary only*, no daemon. The sandbox runs each
# agent command in a disposable sibling container via the mounted host
# socket (see docs/docker-architecture.md). Debian's docker.io did NOT
# put a `docker` binary on PATH on the slim base, so use the official
# static client binary (deterministic, release-independent). The
# `docker --version` line FAILS THE BUILD if the binary is missing, so
# an image can never again silently ship without it.
ARG DOCKER_CLI_VERSION=29.5.1
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) DARCH=x86_64 ;; \
         arm64) DARCH=aarch64 ;; \
         *) DARCH="$ARCH" ;; \
       esac \
    && curl -fsSL -o /tmp/docker.tgz \
       "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
    && tar -xz -C /usr/local/bin --strip-components=1 docker/docker -f /tmp/docker.tgz \
    && rm /tmp/docker.tgz \
    && docker --version

# Non-root user. UID 1000 matches the typical first-host-user UID so the
# named volume lines up without extra chown when bind-mounted.
RUN groupadd --system --gid 1000 mill \
    && useradd --system --gid mill --uid 1000 --create-home --shell /bin/bash mill \
    && mkdir -p /data \
    && chown -R mill:mill /data

WORKDIR /app
COPY . /app
# [dev,tracing]: this image doubles as the test-gate sandbox, so it
# needs pytest + the OpenTelemetry/Langfuse stack to run the project's
# own suite (incl. tracing tests) against an agent's changes.
RUN pip install --no-cache-dir --root-user-action=ignore ".[dev,tracing]" \
    && chown -R mill:mill /app

USER mill
ENV MILL_DATA_DIR=/data
# Bind the API on all interfaces inside the container so a future web
# frontend / published port can reach it; still localhost-only unless
# the port is published in compose.
ENV MILL_API_HOST=0.0.0.0
ENV MILL_API_URL=http://127.0.0.1:8077
EXPOSE 8077

ENTRYPOINT ["/app/entrypoint.sh"]
