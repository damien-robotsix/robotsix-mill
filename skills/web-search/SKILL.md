---
name: Web Search
description: Search the web for current information by delegating to a research sub-agent with live search capability.
when_to_use: Use when you need up-to-date knowledge you are unsure about — current APIs, library versions, recommended integration patterns, breaking changes. Especially before integrating an external library you don't fully know.
---

# Web Search

You do **not** have native web search built into your model. Instead,
use `web_research(query)` — it delegates to a cheap, bounded
sub-agent (the only place where `:online` search and `web_fetch`
live) and returns only a concise factual conclusion.

- Formulate a clear, specific query. The sub-agent searches the web,
  reads relevant sources, and returns a single conclusion with inline
  source URLs.
- Don't implement an unfamiliar library from memory. First use
  `web_research` to find its current, official integration guide, then
  include the exact doc URLs you need fetched.
- Prefer official docs and the library's own repository over blog posts.
- Verify version-specific details (APIs drift) before relying on them.
- Raw search results and full page content never reach your context —
  only the sub-agent's distilled conclusion does. This keeps your
  context lean and avoids search surcharges on your (expensive) model.
