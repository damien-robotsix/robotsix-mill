# Security Policy

This document outlines the security policy for **robotsix/mill**, an
LLM-driven ticket refinement engine that runs agent-chosen shell
commands in disposable Docker sandboxes. If you believe you have found
a security vulnerability, please read on.

## Supported Versions

This project does not have versioned releases. Only the tip of the
`main` branch is supported.

| Branch | Supported          |
| ------ | ------------------ |
| main   | :white_check_mark: |
| others | :x:                |

## Reporting a Vulnerability

**Please do not open a public issue.** Instead, report vulnerabilities
privately through GitHub Security Advisories:

> [Report a vulnerability](https://github.com/damien-robotsix/robotsix-mill/security/advisories/new)

We aim to **acknowledge** your report within **48 hours** and to
deliver a fix for confirmed vulnerabilities within **7 days**. These
timelines are aspirations, not contractual guarantees.

If you do not receive an acknowledgment within 48 hours, feel free to
ping the advisory or reach out to a maintainer directly.

## Scope

A **security vulnerability** is a weakness that lets an attacker
compromise the confidentiality, integrity, or availability of the mill
deployment, the host it runs on, or the secrets it holds.

Concrete threat categories we care about, grounded in the project's
actual architecture:

- **Sandbox container escape** — The implement agent runs LLM-chosen
  shell commands inside sibling Docker containers (`--network none`,
  non-root user, read-only root filesystem, tmpfs `/tmp`, pids/memory
  limits). A vulnerability that breaks out of these containers and
  gains access to the host or to the mill orchestrator container is
  in scope. See `docs/docker-architecture.md` for the full sandbox
  design.

- **Unauthorized access to the management plane API** — The FastAPI
  HTTP API is intentionally unauthenticated and listens on localhost
  only. Any vector that allows a remote (non-localhost) attacker to
  reach the API, or that bypasses the intended localhost restriction,
  is in scope.

- **Secret leakage through logs, API responses, or agent outputs** —
  The mill handles several secrets (OpenRouter API keys, Langfuse
  keys, Docker Hub token, forge personal access token, ntfy token).
  Any path that unintentionally exposes a secret — whether in
  structured logs, trace spans, HTTP response bodies, or the final
  content of tickets produced by agents — is in scope.

- **Supply-chain compromise in the Docker build or the published
  `robotsix/mill` image** — Compromise of build dependencies,
  poisoning of the published image on Docker Hub, or injection of
  malicious code through the CI/CD pipeline that alters the mill
  image are all in scope. The CI workflow
  `.github/workflows/security-audit.yml` runs `pip-audit` on every
  push and weekly to catch known-vulnerable Python dependencies.

- **Prompt injection that escapes sandbox isolation or
  path-confinement** — The agent tools (`read_file`, `write_file`,
  `edit_file`, `list_dir`) are path-confined to a per-ticket git
  clone. Parent-directory, symlink, and absolute-path escapes are
  rejected. A prompt injection that defeats these guards, persuades
  an agent to exfiltrate data via the `web_fetch` tool (the only
  network-enabled path), or otherwise escapes the intended isolation
  boundaries is in scope.

If you are unsure whether an issue qualifies, err on the side of
reporting it through the advisory channel — we would rather triage a
non-issue than miss a real one.

## Disclosure Policy

We follow a **coordinated disclosure** process:

1. You report the vulnerability privately through a GitHub Security
   Advisory.
2. We investigate, reproduce, and develop a fix on a private branch.
3. Once the fix is merged and any necessary mitigations are deployed
   (e.g., a rebuilt Docker image is published), we publish the
   advisory and credit you as the reporter.
4. If you prefer to remain anonymous, please state so in the advisory
   — we will respect that request.

During the investigation window we ask that you keep the details
private. Early public disclosure puts users at risk and undermines the
coordinated process.
