ARG BASE_DIGEST=sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

# ---------------------------------------------------------------------------
# Builder stage — builds the wheel and installs the package
# ---------------------------------------------------------------------------
FROM python:3.12-slim@${BASE_DIGEST} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /build

# git is required at build time: the only non-PyPI dep
# (robotsix-llmio) is a git source in [tool.uv.sources], so uv
# clones it during install. The slim base image has no git.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Bring in the `uv` binary so the install step can honour
# [tool.uv.sources] in pyproject.toml — pip cannot, and the
# only non-PyPI dep (robotsix-llmio) is declared there.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

COPY pyproject.toml .
COPY src/ src/

# --system installs into the image's system Python (the same
# /usr/local/lib/python3.12/site-packages/ path the production
# stage copies from), matching the previous `pip install` layout.
RUN uv pip install --system --no-cache-dir ".[llm]"

# ---------------------------------------------------------------------------
# Production stage — minimal runtime image with only the installed artifacts
# ---------------------------------------------------------------------------
FROM python:3.12-slim@${BASE_DIGEST} AS production

COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /usr/local/bin/robotsix-auto-mail /usr/local/bin/robotsix-auto-mail

RUN groupadd --gid 1000 mailbot && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash mailbot && \
    mkdir -p /home/mailbot/.data /home/mailbot/config && \
    chown mailbot:mailbot /home/mailbot/.data /home/mailbot/config

COPY --chown=mailbot:mailbot entrypoint.sh /usr/local/bin/entrypoint.sh

USER mailbot

# Run from the home directory so relative defaults resolve under it:
# the config file (config/mail.local.yaml) and the SQLite store
# (.data/mail.db) both land in the bind-mounted / persisted locations.
WORKDIR /home/mailbot

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
