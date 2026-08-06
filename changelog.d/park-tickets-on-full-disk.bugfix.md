A full data volume no longer mass-blocks the board. ENOSPC now
classifies as transient, and — mirroring the existing network-outage
parking — a stage that fails on a genuinely full volume PARKS without
consuming a retry attempt, re-polling every
``disk_full_retry_seconds`` until space returns. Previously ENOSPC was
fatal, so one full volume became one hand-resumable BLOCKED ticket per
affected ticket: 146 of them on 2026-08-06, whose retained workspaces
then pinned the disk they were waiting on. A new pre-dispatch admission
gate (``disk_min_free_mb``, default 5 GB) parks tickets *before* a stage
starts, so a stage can no longer die partway through and leave a
half-written workspace consuming the space it just ran out of. Disk
exhaustion arriving as subprocess stderr is detected too, so a
``git clone`` that died on a full disk is no longer reported as an
opaque ``Fatal: CalledProcessError``.
