---
name: Web Search
description: You have live web search built into the model (OpenRouter ":online"). You can look up current information without any tool call.
when_to_use: Use when you need up-to-date knowledge you are unsure about — current APIs, library versions, recommended integration patterns, breaking changes. Especially before integrating an external library you don't fully know.
---

# Web Search

Web search is **native** — there is no tool to call. Simply reason about
what you need to know and state it; the model retrieves current results
automatically and you should cite/incorporate them.

- Don't implement an unfamiliar library from memory. First search for
  its current, official integration guide, then `web_fetch` the exact
  doc page to get precise API names and signatures.
- Prefer official docs and the library's own repository over blog posts.
- Verify version-specific details (APIs drift) before relying on them.
