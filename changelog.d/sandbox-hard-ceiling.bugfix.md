`max_global_concurrency` is now a hard ceiling on live `mill-sbx-*` sandbox
containers, not just on board-consumer stages. It was applied only around
`process_ticket`, so the ~20 per-repo periodic passes, the meta-agent, the
diagnostic pass and refine's warnings collection all spawned sandboxes outside
it — with the cap set to 1, three sandboxes ran at once. The limit now lives in
`sandbox.run()` itself, where the containers are actually created.
