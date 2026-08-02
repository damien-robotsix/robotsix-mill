Scanner findings are grouped under a rollup epic again for all 19 scanner
sources, not 5. #2672 (epic parents for scanner findings) and #2667 (collapse N
findings into one ticket) landed as concurrent PRs and both named their source
set `_SCANNER_SOURCES`, so the second definition silently shadowed the first —
`AUDIT`, `TRACE_HEALTH` and 12 others stopped getting an epic parent. The
narrower set is now `_ROLLUP_SOURCES`. This also clears the `no-redef` mypy
violation that was failing CI on `main` and therefore on every open PR.
