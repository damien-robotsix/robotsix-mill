# Security

This is a personal project built solo with an AI assistant. There are
no releases, no SLAs, and no security guarantees — it's provided as-is.
Only the tip of `main` is ever "supported".

## Reporting something

If you spot a security problem, please **don't open a public issue** —
report it privately via
[GitHub Security Advisories](https://github.com/damien-robotsix/robotsix-mill/security/advisories/new).
I'll look at it when I can; no promised timeline.

## Worth knowing before you run it

mill executes **LLM-chosen shell commands**. It does sandbox them
(disposable Docker containers, `--network none`, non-root, read-only
rootfs — see `docs/docker-architecture.md`) and path-confines the
agent file tools, but: run it in an environment you trust, keep your
API keys/tokens scoped, and don't point it at anything sensitive. The
management API is unauthenticated and localhost-only by design — keep
it that way.

That's it.
