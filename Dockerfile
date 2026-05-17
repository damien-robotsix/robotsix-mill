FROM python:3.14-slim

# git: the implement stage branches/commits on per-ticket work trees.
# Acquire::Retries lets apt recover from transient mirror glitches.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
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
ARG DOCKER_CLI_VERSION=27.3.1
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) DARCH=x86_64 ;; \
         arm64) DARCH=aarch64 ;; \
         *) DARCH="$ARCH" ;; \
       esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
    && docker --version

# Non-root user. UID 1000 matches the typical first-host-user UID so the
# named volume lines up without extra chown when bind-mounted.
RUN groupadd --system --gid 1000 mill \
    && useradd --system --gid mill --uid 1000 --create-home --shell /bin/bash mill \
    && mkdir -p /data \
    && chown -R mill:mill /data

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir --root-user-action=ignore . \
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
