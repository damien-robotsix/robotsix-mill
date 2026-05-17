FROM python:3.14-slim

# git: the implement stage branches/commits on per-ticket work trees.
# docker CLI (client only — no daemon): the sandbox runs each agent
#   command in a disposable sibling container via the mounted host
#   socket (see docker-compose.yml).
# Acquire::Retries lets apt recover from transient mirror glitches.
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*

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
