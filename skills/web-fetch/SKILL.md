---
name: Web Fetch
description: Fetch the exact text of a known http(s) URL — official docs, a raw source file, package metadata, an API JSON response.
when_to_use: Use when you need the precise content of a specific page or file (e.g. the Langfuse OpenTelemetry docs, a library's source on raw.githubusercontent.com, a PyPI/npm metadata JSON). For open-ended "what is the current best way to..." questions, prefer web search instead (you have it natively).
---

# Web Fetch

You have a `web_fetch(url)` tool. Call it with a single http(s) URL; it
returns the page/file body as text (size-capped).

- **Do not** try to `curl`/`wget` via `run_command` — your command
  sandbox has **no network**. Only `web_fetch` can reach the internet.
- Fetch authoritative sources directly: official documentation pages,
  `raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>` for source,
  `https://pypi.org/pypi/<pkg>/json` for package info.
- Read before you code: when integrating a library (e.g. Langfuse,
  OpenTelemetry, pydantic-ai instrumentation), fetch its current docs
  and mirror the documented API rather than guessing.
- One URL per call. Follow links by fetching them in turn.
- It returns an error string (never raises) — on failure, try a
  different URL or fall back to web search.
