# Deploy integration

The deploy module provides two deploy-time safeguards:
a **worker-image freshness check** that prevents retrying tickets on a
stale worker, and a **config-standard footprint validator** that blocks
deployments carrying stray `_standards/` copies.

## Worker-image freshness check

`check_deploy_freshness()` queries the deploy server's
`GET /services/mill` endpoint to compare the running and latest image
digests.  When the two diverge — a newer image was pushed but the
worker hasn't been re-deployed yet — the gate reports
`update_available: true`.

### Callers

The **implement preflight** and **resume-blocked** paths call
`check_deploy_freshness()` before burning an agent attempt.  If the
worker is stale, retrying a previously-blocked ticket is likely to fail
with the same error (the fix hasn't reached the running image), so the
gate short-circuits the attempt.

The check is intentionally lenient: when `deploy_api_url` is unset
(freshness gate disabled), or when the deploy server is unreachable
(transient infra failure), it returns `None` rather than blocking the
ticket.

## Config-standard footprint validation

`validate_config_standard_footprint()` is a **deploy-time gate** called
before pushing a commit to the target repo.  It scans the repo tree for
a stray local `_standards/` copy of the standards contract.  The
canonical standard/doc sources are `robotsix-config` and
`robotsix-standards` — individual repos must **not** carry local
`_standards/` copies.

When `changed_files` is provided, only violations that appear in the
diff are flagged — files that pre-date the current ticket are excluded.
This prevents the gate from blocking tickets whose diff is clean but
whose repo carries historical `_standards/` cruft.
