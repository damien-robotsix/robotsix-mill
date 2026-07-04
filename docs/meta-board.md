# Meta board

The **meta board** is a synthetic board (`board_id: "meta"`) that the
meta-agent uses to file and manage **cross-repo proposals** — tickets
that span multiple repositories (e.g. extraction proposals, repo
scaffolding, cross-cutting changes).  Unlike regular per-repo boards,
the meta board has no backing forge repository — it exists purely in
the ticket system and is driven by the meta-agent's reasoning over
clone data.

## Registration

No registration is needed: the meta board is synthetic and is
constructed automatically (`repo_id`/`board_id` `"meta"`).  It does
not appear under the `repos:` key of `config/config.json` and has no
clone or forge remote.

### Langfuse credentials

Langfuse is configured **globally** (the `langfuse_*` entries of the
`secrets:` block in `config/config.json`); the meta board — like every
repo — is populated from those credentials at load time.  When they
are absent, the meta-agent simply runs untraced.

### Verify

```sh
python scripts/verify_repos_config.py
```

## Usage

The meta board is available automatically:

```python
from robotsix_mill.ticket_service import TicketService
svc = TicketService(board_id="meta")
```

No code changes are needed — `TicketService` resolves `board_id="meta"`
against the repos registry and creates tickets on the synthetic board.
The meta-agent runner wires this together at runtime.

## Safety guards

### Repo-triage fallback protection

When triage cannot confidently match a target repository (the agent
output is empty or names only unknown repos), the system falls back to
cloning *every* clonable repo — a safe default so the work is at least
possible. However, this fallback is inherently ambiguous: if a
brand-new repo should be created and populated in a single ticket, the
triage fallback will not know about the brand-new target and clone
every other repo instead, causing the output to be misrouted to an
arbitrarily-chosen primary repo.

To prevent this misrouting, the **deliver stage blocks delivery** when:
1. Triage fell back to cloning all repos, AND
2. The branch creates brand-new top-level files (files in the repo
   root, not in subdirectories)

If you see this block, check whether:
- The ticket should target a specific registered repo (name it
  explicitly in the spec if triage is guessing wrong).
- The ticket creates a brand-new repo that is not yet registered in
  `config/repos.yaml` — register it first, then re-run deliver.

Genuine cross-repo audits and multi-repo refactors (where every repo
should be touched) are not affected — if the spec explicitly names all
target repos, triage will match them and the guard does not block.

### Epic child-dependency ordering

When an epic is decomposed into children and one child is a
"create/initialize repository" action (detected by keywords like
"Initialize communication system repository" or "Create repo X"),
other repo-populating children automatically depend on that
initialization child. This ensures the new repo is registered and
available *before* any child tries to write into it.

The ordering is automatic — no manual intervention needed. The epic
breakdown agent lists children in the intended order, and the system
automatically wires dependencies so that:
- The init-repo child runs first.
- Populating children stay blocked (`unmet_deps`) until the init-repo
  child closes and the repo is registered.
- Children that don't populate a repo proceed in their original order.
