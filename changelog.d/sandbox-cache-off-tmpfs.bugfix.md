Sandbox `uv`/`pip` caches now live on a shared disk-backed volume subpath
instead of the sandbox's `/tmp`. `HOME=/tmp` is a tmpfs — RAM charged to the
container's own memory limit — so ever since the test gate began installing the
project, each sandbox spent its memory budget caching the dependency tree
(measured: 625 MB of `/tmp/.cache` in one live sandbox, another pinned at
1022 MiB against a 1 GiB cap). The tmpfs is also size-bounded now, so an
overflow fails with `ENOSPC` rather than an unexplained OOM kill, and the
sandbox-reaper pass drops the shared cache once it exceeds its budget.
