---
name: ask_user_guardrails
---

## Asking the operator for help

You have an `ask_user` tool. Calling it pauses the ticket until the
operator replies. Your question is posted as an `[ASK_USER]` comment on
the ticket. When the operator replies, the ticket resumes with their
answer injected into your prompt.

Because `ask_user` blocks the ticket and demands human attention, be
deliberate about when you reach for it.

### ✅ Good reasons to ask

- **Genuine ambiguity a single sentence resolves.** E.g. the spec says
  "integrate with the auth service" but the repo has two auth services.
- **A design decision the operator owns.** E.g. "Should the default sort
  be ascending or descending?" — the choice changes user-visible behaviour.
- **A contradiction in the spec that reading source files cannot resolve.**
  E.g. the spec says "use port 8080" in one place and "port 3000" in another.
- **(Refine only) The draft is too vague to derive intent even after
  exploring.** Asking is better than inventing a spec the operator didn't want.
- **(Implement only) A blocking ambiguity that must be resolved before
  any code is written.** E.g. the spec names two incompatible libraries.

### ❌ Bad reasons to ask

- **Anything you can answer by reading more files.** Exhaust your
  read and exploration tools first.
- **Anything you can reasonably infer** from the existing spec, codebase
  conventions, or surrounding context. When in doubt, infer and move on.
- **Implementation details that are yours to decide:** variable names,
  helper-function extraction, error-message wording — these are your
  responsibility.
- **Questions about your own constraints or capabilities documented in
  the repo.** E.g. "should I use stdlib-only?", "what tools do I have?".
  Read `agent_definitions/`, `skills/`, or your system prompt first.
- **(Implement only) Ambiguities that don't block progress.** If you can
  pick a reasonable default and keep coding, do that and note the
  assumption in your summary.

### Examples

**Ask — refine:** "The draft says 'add a config option for timeout' but
there are two timeout configs (connect, read). Which one should this
replace, or is it a new third timeout?"

**Forge ahead — refine:** "The draft says 'add sorting' but doesn't
specify the sort key. The existing list endpoint sorts by `created_at`;
infer that as the default."

**Ask — implement:** "The spec says 'use the existing cache layer' but
the repo has both Redis and an in-memory cache — which one?"

**Forge ahead — implement:** "The spec says 'add error handling' but
doesn't say which exception class. The codebase consistently uses
`APIError` for HTTP-facing errors; use that."
