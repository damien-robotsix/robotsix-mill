# Security model

> Full container topology (mill vs. sibling sandbox, the three code
> copies, the docker.sock trust boundary):
> [docs/docker-architecture.md](docker-architecture.md).

The `implement` agent runs LLM-chosen shell commands, and ticket text /
cloned repo content can steer that LLM (prompt injection). So command
execution is isolated from the mill process:

- **File tools** (`read_file`/`write_file`/`edit_file`/`list_dir`) run in-process
  but are **path-confined** to the ticket's clone (`..`/symlink/abs
  escapes are rejected).
- **Command execution** (`run_command` and the test command) **always**
  runs in a fresh, disposable sibling container — `--network none`,
  `--rm`, non-root, read-only root + tmpfs `/tmp`, pids/memory capped,
  only the ticket's repo reachable. Needs the host Docker socket
  (root-equivalent on the host — see `docker-compose.yml`). There is
  **no in-process/local mode**: it was a foot-gun that let the agent
  edit the host and recursively re-invoke the pipeline. Tests fake the
  sandbox seam instead.

## See also

- [index.md](index.md) — documentation home
- [docs/docker-architecture.md](docker-architecture.md) — container topology
- [docs/forge/github-app.md](forge/github-app.md) — delivery identity setup
