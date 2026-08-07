The post-rebase drop guard no longer blocks tickets over registry and
boilerplate files. `docs/modules.yaml` and `site/modules.yaml` join the
changelog paths already exempt, and the list is now the
`rebase_drop_exempt_paths` setting rather than a hardcoded tuple. These files
are a function of the whole repo and are re-derived by CI, so a rebase settles
them on a version matching neither the branch nor the target — a case the
blob-equality excuse cannot clear, which reported healthy reconciliations as
silent drops.
