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
