# Meta board

The **meta board** is a synthetic board (`board_id: "meta"`) that the
meta-agent uses to file and manage **cross-repo proposals** — tickets
that span multiple repositories (e.g. extraction proposals, repo
scaffolding, cross-cutting changes).  Unlike regular per-repo boards,
the meta board has no backing forge repository — it exists purely in
the ticket system and is driven by the meta-agent's reasoning over
clone data.

## Registration

The meta board is just a `repos.yaml` entry.  Copy the `meta` stanza
from `config/repos.example.yaml` into your host `config/repos.yaml`:

```yaml
repos:
  meta:
    board_id: "meta"
    langfuse:
      project_name: "robotsix-mill"
      public_key: "pk-lf-..."     # copy your robotsix-mill public key
      secret_key: "sk-lf-..."     # copy your robotsix-mill secret key
      base_url: "https://cloud.langfuse.com"  # same as mill
```

### Langfuse credentials

The meta board reuses the **same Langfuse project** as robotsix-mill
itself — not a per-repo project.  Copy the `public_key`, `secret_key`,
and `base_url` from your mill `Secrets` / Langfuse config.  This
ensures meta-agent traces land in the mill project alongside other
mill-internal observability data.

### Verify

```sh
python scripts/verify_repos_config.py
```

## Usage

Once registered, the meta board is available automatically:

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
