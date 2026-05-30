ARG BASE_DIGEST=sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

# ---------------------------------------------------------------------------
# Builder stage — builds the wheel and installs the package
# ---------------------------------------------------------------------------
FROM python:3.12-slim@${BASE_DIGEST} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /build

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir ".[llm]"

# ---------------------------------------------------------------------------
# Production stage — minimal runtime image with only the installed artifacts
# ---------------------------------------------------------------------------
FROM python:3.12-slim@${BASE_DIGEST} AS production

COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /usr/local/bin/robotsix-auto-mail /usr/local/bin/robotsix-auto-mail

RUN groupadd --gid 1000 mailbot && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash mailbot && \
    mkdir -p /home/mailbot/data /home/mailbot/config && \
    chown mailbot:mailbot /home/mailbot/data /home/mailbot/config

COPY --chown=mailbot:mailbot entrypoint.sh /usr/local/bin/entrypoint.sh

USER mailbot

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
