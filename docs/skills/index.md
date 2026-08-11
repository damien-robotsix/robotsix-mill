# Skills

Skill files are Markdown documents that teach agents specific workflows
or impose guardrails. They are **injected directly into an agent's
system prompt** at the start of every run, appearing under a
`## Skills` heading.

## How skills are loaded

1. An agent definition YAML (e.g. `agent_definitions/implement.yaml`)
   declares `skills: [ask_user_guardrails, board-read, board-report]`.
2. Before the agent runs, a preflight check
   (`src/robotsix_mill/stages/implement/phase_coordinator_preflight.py`)
   verifies every referenced skill file exists.  Missing skills block
   the run.
3. `compose_prompt()` in `src/robotsix_mill/agents/base.py` reads
   `<skills_dir>/<name>/SKILL.md` for each skill, strips the YAML
   front-matter (`--- … ---`), and appends the body to the system
   prompt.

## Naming convention

Each skill lives in its own subdirectory under `skills/` and contains
a single `SKILL.md` file:

```
skills/
├── ask_user_guardrails/
│   └── SKILL.md
├── board-read/
│   └── SKILL.md
└── board-report/
    └── SKILL.md
```

The `SKILL.md` file uses YAML front-matter with a **single required
field** — `name:` — whose value must match the directory name:

```markdown
---
name: ask_user_guardrails
---

## Asking the operator for help

…
```

The directory name, the `name:` field, and the value in an agent
definition's `skills:` list must all agree — `compose_prompt` uses the
name from the `skills:` list to resolve `<skills_dir>/<name>/SKILL.md`.

## Adding a new skill

1. Create a new directory under `skills/` (e.g.
   `skills/my-new-skill/`).
2. Add a `SKILL.md` inside it with YAML front-matter:

   ```markdown
   ---
   name: my-new-skill
   ---

   ## My New Skill

   Skill body here.
   ```

3. Declare the skill in `agent_definitions/<agent>.yaml` by adding its
   name to the `skills:` list — or to `expert_definitions/<expert>.yaml`
   for expert sub-agents.
4. Update `docs/modules.yaml` — add `skills/my-new-skill/**/*` to
   the `skills` module's `paths` list.
