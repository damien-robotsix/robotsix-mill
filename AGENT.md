# AGENT.md — instructions for any agent (human or AI) working in this repo

This is **robotsix/mill**, a personal project built solo with an AI
assistant. Optimize for a small, sharp, honest codebase — not
enterprise process. These are hard rules, learned the hard way.

## Scope & taste

- **Proportionate scope.** This is a solo hobby project. No SLAs, no
  formal policies, no compliance ceremony, no speculative
  "enterprise-grade" abstractions. Right-size everything (see the
  trimmed `SECURITY.md` for the target tone).
- **One ticket = one focused change.** If a ticket bundles N things
  (e.g. "checksum + cleanup + HEALTHCHECK + multi-stage"), it's too
  big — split it. Prefer the smallest change that solves the problem.
- Match the style, comment density, and idioms of the surrounding
  code. Don't restate what the code or git history already says.

## The test gate is sacred and must stay hermetic

- The implement stage gates on the **full pytest suite running inside
  the container**. It must be green there, not just on a dev machine.
- **Tests never touch the network and never consume tokens.**
  `tests/conftest.py` strips every credential/endpoint env var and
  hard-blocks real `httpx` transports. Keep it that way. Always mock
  the model / HTTP seam (`build_agent`, the `run_*_agent` seam, or
  `httpx`). A test that needs a real key or a real request is wrong.
- Run the suite before you commit. Add/adjust tests with the change.

## Board UI

- The kanban CSS/JS live in `src/robotsix_mill/runtime/static/`
  (`board.css`, `board.js`), served via `StaticFiles`.
- **Never inline JS/CSS back into the `board_html.py` Python string.**
  A `\n` in a Python-embedded JS string becomes a real newline and
  silently breaks the entire board. Put JS in `board.js`.

## Git / CI

- `git fetch && git rebase origin/main` **before** committing. The
  mill merges autonomously; assume `main` moved.
- **Never weaken a quality/security gate to make CI go green.** Don't
  lower a Trivy severity, flip an `exit-code`, relax a lint threshold,
  or broaden an ignore. Fix the real cause, or add a *narrow,
  justified, commented* ignore entry.
- Don't reintroduce a regression a test or this file already guards.

## Agent behavior

- `report_issue` is for a real blocking/degrading problem you hit
  while working — never a "nothing to report / clean run" no-op.
- Respect the sandbox and path-confinement; never bypass isolation or
  exfiltrate secrets. The management API stays unauthenticated +
  localhost-only by design.
- If something is genuinely underspecified or a tool is missing, say
  so (or `report_issue`) — don't guess and gold-plate.

## Reference docs: `agent_references/`

Stack-specific gotchas live under `agent_references/` — one Markdown
file per topic (e.g. `agent_references/sqlalchemy-sqlite.md`). They
are **not** auto-injected into any agent's prompt; an agent that is
about to touch a stack covered there is expected to `read_file` the
matching entry first. Spec writers (refine) should NOT pre-prescribe
the workaround — let the implement agent consult the reference when
it has the actual code in front of it.

When you discover a new stack-level trap that another agent will hit:
add a new `agent_references/<topic>.md` describing it in the same
shape as the existing entry (limitation → consequence → canonical
workaround). Keep entries narrow and verifiable in the repo.
