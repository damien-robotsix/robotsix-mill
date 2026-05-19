---
name: Web Fetch
description: Retrieve the content of a known URL by delegating to a research sub-agent that can fetch pages.
when_to_use: Use when you need the precise content of a specific page or file (e.g. the Langfuse OpenTelemetry docs, a library's source on raw.githubusercontent.com, a PyPI/npm metadata JSON). For open-ended "what is the current best way to..." questions, prefer web search instead (see the Web Search skill).
---

# Web Fetch

You do **not** have a direct `web_fetch(url)` tool. Instead, use
`web_research(query)` — it delegates to a cheap, bounded sub-agent
(the only place where `web_fetch` and live web search live) and
returns a concise factual conclusion.

- Include the exact URL(s) you want fetched in your query, plus the
  specific information to extract from them.
- The sub-agent can fetch from any http(s) URL — official docs,
  `raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>` for source,
  `https://pypi.org/pypi/<pkg>/json` for package info.
- **Do not** try to `curl`/`wget` via `run_command` — your command
  sandbox has **no network**. Only the sub-agent can reach the internet.
- Read before you code: when integrating a library (e.g. Langfuse,
  OpenTelemetry, pydantic-ai instrumentation), use `web_research` to
  fetch its current docs rather than guessing.
- The sub-agent returns an error string on failure (never raises) —
  on failure, try a different URL or rephrase your query.
